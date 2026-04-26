"""
Payout service — this is where the hard guarantees live.

Three invariants enforced here:
1. Balance never goes negative (checked under a row-level lock)
2. Two concurrent payouts for the same merchant cannot both succeed (SELECT FOR UPDATE)
3. State machine transitions are enforced (COMPLETED/FAILED are terminal)
"""
from decimal import Decimal
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.models.models import (
    Merchant, LedgerEntry, Payout, PayoutStatus, TransactionType
)


class InsufficientFundsError(Exception):
    pass


class InvalidStateTransitionError(Exception):
    pass


class MerchantNotFoundError(Exception):
    pass


def get_balance_for_update(db: Session, merchant_id: str) -> Decimal:
    """
    Calculate merchant balance while holding a row-level lock on the merchant row.

    The SELECT FOR UPDATE on merchants means:
      - Only one transaction can hold this lock at a time.
      - Any other transaction trying to payout for the same merchant will block
        until the first transaction commits or rolls back.
      - This prevents the classic TOCTOU race: two threads both see balance=100,
        both decide they can pay out 80, both debit — leaving balance at -60.

    Why aggregate on ledger_entries and not lock ledger_entries?
      Locking the merchant row is sufficient and cheaper. Ledger entries are
      append-only; the only mutation that matters is the debit we're about to
      insert. Locking the parent merchant row serializes all payout attempts
      for that merchant without locking the entire ledger table.
    """
    # Lock the merchant row — this is the critical section gate
    merchant = (
        db.query(Merchant)
        .filter(Merchant.id == merchant_id)
        .with_for_update()  # SELECT FOR UPDATE
        .first()
    )
    if not merchant:
        raise MerchantNotFoundError(f"Merchant {merchant_id} not found")

    # Aggregate while we hold the lock — safe from concurrent mutation
    result = db.execute(
        text("""
            SELECT
                COALESCE(SUM(CASE WHEN type = 'credit' THEN amount ELSE 0 END), 0) -
                COALESCE(SUM(CASE WHEN type = 'debit'  THEN amount ELSE 0 END), 0)
                AS balance
            FROM ledger_entries
            WHERE merchant_id = :merchant_id
        """),
        {"merchant_id": merchant_id},
    ).fetchone()

    return Decimal(result.balance)


def get_balance(db: Session, merchant_id: str) -> Decimal:
    """
    Read-only balance query — no lock. Used for GET /merchants/{id}/balance.
    """
    result = db.execute(
        text("""
            SELECT
                COALESCE(SUM(CASE WHEN type = 'credit' THEN amount ELSE 0 END), 0) -
                COALESCE(SUM(CASE WHEN type = 'debit'  THEN amount ELSE 0 END), 0)
                AS balance
            FROM ledger_entries
            WHERE merchant_id = :merchant_id
        """),
        {"merchant_id": merchant_id},
    ).fetchone()
    return Decimal(result.balance)


def create_payout(db: Session, merchant_id: str, amount: Decimal) -> Payout:
    """
    Attempt a payout. The sequence:

    1. BEGIN (called by the route handler via db transaction)
    2. SELECT ... FOR UPDATE on merchants row  ← acquires lock
    3. Aggregate balance from ledger_entries   ← safe under lock
    4. Check balance >= amount
    5. INSERT payout row (status=PROCESSING)
    6. INSERT ledger debit entry
    7. COMMIT                                  ← releases lock

    If step 4 fails → raise InsufficientFundsError, transaction rolled back.
    If any step throws → transaction rolled back, lock released.
    """
    amount = Decimal(str(amount))

    # Step 2+3: Lock merchant row, then calculate balance
    balance = get_balance_for_update(db, merchant_id)

    # Step 4: Funds check — inside the lock
    if balance < amount:
        raise InsufficientFundsError(
            f"Insufficient funds: balance={balance}, requested={amount}"
        )

    # Step 5: Create payout record directly in PROCESSING state
    # We skip PENDING here for machine-initiated payouts; the payout is
    # atomic with the ledger debit so there is no intermediate visible state.
    payout = Payout(
        merchant_id=merchant_id,
        amount=amount,
        status=PayoutStatus.PROCESSING,
    )
    db.add(payout)
    db.flush()  # get payout.id before inserting ledger entry

    # Step 6: Write the debit entry — immutable, append-only
    debit = LedgerEntry(
        merchant_id=merchant_id,
        type=TransactionType.DEBIT,
        amount=amount,
        description="Payout",
        reference_id=payout.id,
        payout_id=payout.id,
    )
    db.add(debit)

    # Step 7: Commit happens in route handler — releases the FOR UPDATE lock
    return payout


def complete_payout(db: Session, payout_id: str) -> Payout:
    """
    Transition a payout from PROCESSING → COMPLETED.

    State machine check: only PROCESSING payouts can complete.
    COMPLETED and FAILED are terminal states — this is enforced explicitly
    rather than relying on caller discipline.
    """
    payout = db.query(Payout).filter(Payout.id == payout_id).with_for_update().first()
    if not payout:
        raise ValueError(f"Payout {payout_id} not found")

    # ── STATE MACHINE GUARD ─────────────────────────────────────────────────
    # This is the exact check that blocks failed-to-completed transitions.
    # COMPLETED and FAILED are both terminal; neither can move to COMPLETED.
    if payout.status != PayoutStatus.PROCESSING:
        raise InvalidStateTransitionError(
            f"Cannot complete payout in state '{payout.status}'. "
            f"Only PROCESSING payouts can be completed."
        )
    # ────────────────────────────────────────────────────────────────────────

    payout.status = PayoutStatus.COMPLETED
    payout.updated_at = datetime.utcnow()
    return payout


def fail_payout(db: Session, payout_id: str, reason: str) -> Payout:
    """
    Transition a payout from PROCESSING → FAILED.
    Reverses the debit by inserting a compensating credit entry.
    """
    payout = db.query(Payout).filter(Payout.id == payout_id).with_for_update().first()
    if not payout:
        raise ValueError(f"Payout {payout_id} not found")

    # State machine guard — only PROCESSING can fail
    if payout.status != PayoutStatus.PROCESSING:
        raise InvalidStateTransitionError(
            f"Cannot fail payout in state '{payout.status}'. "
            f"Only PROCESSING payouts can be failed."
        )

    payout.status = PayoutStatus.FAILED
    payout.failure_reason = reason
    payout.updated_at = datetime.utcnow()

    # Compensating entry: reverse the debit so the merchant's balance is restored
    reversal = LedgerEntry(
        merchant_id=payout.merchant_id,
        type=TransactionType.CREDIT,
        amount=payout.amount,
        description=f"Payout reversal: {reason}",
        reference_id=payout.id,
        payout_id=payout.id,
    )
    db.add(reversal)
    return payout
