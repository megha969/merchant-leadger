# EXPLAINER.md

## 1. The Ledger

### Balance calculation query

```sql
SELECT
    COALESCE(SUM(CASE WHEN type = 'credit' THEN amount ELSE 0 END), 0) -
    COALESCE(SUM(CASE WHEN type = 'debit'  THEN amount ELSE 0 END), 0)
    AS balance
FROM ledger_entries
WHERE merchant_id = :merchant_id
```

Found in `app/services/payout_service.py` → `get_balance()` and `get_balance_for_update()`.

### Why credits and debits are modeled this way

Every entry in `ledger_entries` is **immutable and append-only**. We never UPDATE an amount. We never DELETE a row. If money moves in, we insert a credit row. If money moves out, we insert a debit row. If a payout fails and needs reversal, we insert a compensating credit — we do not delete the original debit.

**Why not a single `balance` column on the merchant?**

A single balance column requires a read-then-write: read current balance, add/subtract, write back. Under concurrency this is a classic TOCTOU race — two transactions read the same value and both write back conflicting results. You can serialize with locks, but then you've lost the audit trail anyway.

The ledger model gives you:
- **Auditability**: every cent movement is a permanent record with a timestamp and description.
- **Correctness by aggregation**: balance is derived from facts, not maintained state. There is no way to corrupt it silently.
- **Replayability**: drop the balance column entirely and recompute from entries. The ledger is the source of truth.
- **Reversal without deletion**: a failed payout inserts a compensating credit. The original debit stays. You can see the full history of what happened and why.

Amounts are always positive. The `type` column (credit/debit) determines direction. This avoids sign-convention bugs where negative debits and positive credits create ambiguous arithmetic.

---

## 2. The Lock

### Exact code that prevents two concurrent payouts from overdrawing

```python
# app/services/payout_service.py → get_balance_for_update()

def get_balance_for_update(db: Session, merchant_id: str) -> Decimal:
    # Lock the merchant row — this is the critical section gate
    merchant = (
        db.query(Merchant)
        .filter(Merchant.id == merchant_id)
        .with_for_update()          # ← SELECT FOR UPDATE
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
```

And in `create_payout()`, the full sequence:

```python
def create_payout(db: Session, merchant_id: str, amount: Decimal) -> Payout:
    # 1. Acquire row-level lock + read balance atomically
    balance = get_balance_for_update(db, merchant_id)

    # 2. Funds check — inside the lock
    if balance < amount:
        raise InsufficientFundsError(...)

    # 3. Insert payout record
    payout = Payout(merchant_id=merchant_id, amount=amount, status=PayoutStatus.PROCESSING)
    db.add(payout)
    db.flush()

    # 4. Insert debit entry
    debit = LedgerEntry(merchant_id=merchant_id, type=TransactionType.DEBIT, amount=amount, ...)
    db.add(debit)

    # 5. Caller commits → lock released
    return payout
```

### What database primitive it relies on

**`SELECT FOR UPDATE`** — a PostgreSQL row-level exclusive lock.

When Transaction A executes `SELECT ... FOR UPDATE` on a merchant row, PostgreSQL grants it an exclusive lock on that row. Transaction B, attempting the same `SELECT ... FOR UPDATE` on the same row, **blocks** at the database level until Transaction A commits or rolls back.

This means:
- Thread 1 reads balance=100, decides it can pay out 80, debits 80, commits. Lock released. Balance is now 20.
- Thread 2 was blocked the entire time. It now reads balance=20. It wanted to pay out 80. It fails with InsufficientFunds.

Without `FOR UPDATE`, both threads read balance=100 simultaneously, both decide 80 is fine, both insert a debit for 80. Balance ends up at -60. That's the classic overdraft race.

**Why lock the merchant row, not the ledger_entries table?**

Ledger entries are append-only. The mutation that matters is the debit we're about to insert. By locking the merchant row (a single row per merchant), we serialize all payout attempts for that merchant without locking the entire ledger table. Any number of read-only balance queries can proceed concurrently — they don't take the lock.

**Why not Python-level locking (threading.Lock)?**

Python locks die with the process. They don't survive a dyno restart, a second worker, or a horizontal scale event. The database lock works regardless of how many processes or threads are hitting the API simultaneously.

---

## 3. The Idempotency

### How the system knows it has seen a key before

We attempt to `INSERT` a row into `idempotency_keys` with a `UNIQUE` constraint on `(key, merchant_id)`.

```python
# app/services/idempotency_service.py → acquire_idempotency_key()

record = IdempotencyKey(key=key, merchant_id=merchant_id, status="in_flight", ...)
db.add(record)
try:
    db.flush()      # triggers the unique constraint check
    return record   # first time we've seen this key — proceed
except IntegrityError:
    db.rollback()   # constraint violation — key exists

# Row exists — check its state
existing = db.query(IdempotencyKey).filter(...).first()

if existing.status == "completed":
    raise IdempotencyReplay(status_code=..., body=json.loads(existing.response_body))

# status == "in_flight"
raise IdempotencyConflict("Request ... is already in flight. Retry after a short delay.")
```

The `INSERT` is the check. There is no separate SELECT-then-INSERT pattern, which would have its own race window. The database's unique constraint makes the check-and-claim atomic.

After the business logic completes, we mark the key `completed` and store the serialized HTTP response:

```python
def complete_idempotency_key(db, record, status_code, response_body):
    record.status = "completed"
    record.response_status = str(status_code)
    record.response_body = json.dumps(response_body)
    record.completed_at = datetime.utcnow()
```

On replay, the stored response is returned byte-for-byte. The client cannot tell whether it received a fresh or replayed response.

### What happens if the first request is in-flight when the second arrives

The second request hits the `INSERT`, gets an `IntegrityError`, rolls back its savepoint, then queries the existing row. It finds `status = "in_flight"` and raises `IdempotencyConflict`, which the route handler converts to **HTTP 409 Conflict** with the message:

> "Request with idempotency key '...' is already in flight. Retry after a short delay."

The second request does **not** execute the business logic. There is no double payout. The client is told to wait and retry — at which point the first request will have either completed (and the response will be replayed) or failed (and the key will have been cleaned up or left in a failed state).

This avoids the alternative design where the second request blocks waiting for the first. Blocking is dangerous: it can cause thread exhaustion under load, and it couples two requests' lifetimes together. Returning 409 immediately keeps the server stateless and lets the client control retry timing.

---

## 4. The State Machine

### Where failed-to-completed is blocked

`app/services/payout_service.py` → `complete_payout()`:

```python
def complete_payout(db: Session, payout_id: str) -> Payout:
    payout = db.query(Payout).filter(Payout.id == payout_id).with_for_update().first()
    if not payout:
        raise ValueError(f"Payout {payout_id} not found")

    # ── STATE MACHINE GUARD ──────────────────────────────────────────────
    # This is the exact check that blocks failed-to-completed transitions.
    # COMPLETED and FAILED are both terminal; neither can move to COMPLETED.
    if payout.status != PayoutStatus.PROCESSING:
        raise InvalidStateTransitionError(
            f"Cannot complete payout in state '{payout.status}'. "
            f"Only PROCESSING payouts can be completed."
        )
    # ────────────────────────────────────────────────────────────────────

    payout.status = PayoutStatus.COMPLETED
    payout.updated_at = datetime.utcnow()
    return payout
```

The valid transitions are:

```
PENDING → PROCESSING → COMPLETED
                  ↘
                   FAILED
```

COMPLETED and FAILED are terminal. The guard `payout.status != PayoutStatus.PROCESSING` catches every invalid transition: `FAILED → COMPLETED`, `COMPLETED → COMPLETED`, `PENDING → COMPLETED`. The same pattern exists in `fail_payout()` for the symmetric case.

The route handler converts `InvalidStateTransitionError` to HTTP 422.

The lock (`with_for_update()`) on the payout row here prevents a race where two threads simultaneously try to transition the same payout — one completing and one failing. The first to acquire the lock wins; the second sees the already-transitioned status and raises.

---

## 5. The AI Audit

### What AI gave me, what I caught, and what I replaced it with

**Prompt I gave:** "Write a function to safely create a payout, checking the balance and inserting a debit."

**What the AI generated:**

```python
def create_payout(db: Session, merchant_id: str, amount: Decimal) -> Payout:
    # Check current balance
    merchant = db.query(Merchant).filter(Merchant.id == merchant_id).first()
    
    credits = db.query(func.sum(LedgerEntry.amount)).filter(
        LedgerEntry.merchant_id == merchant_id,
        LedgerEntry.type == TransactionType.CREDIT
    ).scalar() or 0
    
    debits = db.query(func.sum(LedgerEntry.amount)).filter(
        LedgerEntry.merchant_id == merchant_id,
        LedgerEntry.type == TransactionType.DEBIT
    ).scalar() or 0
    
    balance = Decimal(str(credits)) - Decimal(str(debits))
    
    if balance < amount:
        raise InsufficientFundsError("Insufficient funds")
    
    payout = Payout(merchant_id=merchant_id, amount=amount, status=PayoutStatus.PROCESSING)
    db.add(payout)
    db.flush()
    
    debit = LedgerEntry(merchant_id=merchant_id, type=TransactionType.DEBIT, amount=amount, ...)
    db.add(debit)
    return payout
```

**What's wrong with it:**

The balance check and the debit insert are two separate operations with no lock between them. This is a textbook TOCTOU (Time-of-Check to Time-of-Use) race condition.

Two concurrent requests both enter this function. Both execute the balance aggregation query and both see balance=100. Both check `100 >= 80` and both pass. Both insert a debit for 80. The database happily accepts both inserts because there is no constraint preventing it. The merchant's balance is now -60.

The AI produced code that *looks* correct in single-threaded testing and *looks* like it has a balance check. It would pass any test suite that doesn't exercise concurrency. It would silently fail in production the moment two payout requests arrive close together for the same merchant — which is exactly when payouts tend to arrive (batch processing, retries, double-clicks).

**What I replaced it with:**

```python
def create_payout(db: Session, merchant_id: str, amount: Decimal) -> Payout:
    # Lock the merchant row BEFORE reading the balance.
    # Any other transaction attempting this for the same merchant blocks here
    # until we commit or roll back.
    merchant = (
        db.query(Merchant)
        .filter(Merchant.id == merchant_id)
        .with_for_update()   # ← this is what the AI missed
        .first()
    )
    
    # NOW aggregate — we hold the lock, so this reflects committed reality
    result = db.execute(
        text("""
            SELECT
                COALESCE(SUM(CASE WHEN type = 'credit' THEN amount ELSE 0 END), 0) -
                COALESCE(SUM(CASE WHEN type = 'debit'  THEN amount ELSE 0 END), 0)
                AS balance
            FROM ledger_entries WHERE merchant_id = :merchant_id
        """),
        {"merchant_id": merchant_id},
    ).fetchone()
    
    balance = Decimal(result.balance)
    
    if balance < amount:
        raise InsufficientFundsError(...)
    
    # Insert payout + debit while still holding the lock
    payout = Payout(...)
    db.add(payout)
    db.flush()
    db.add(LedgerEntry(type=TransactionType.DEBIT, ...))
    return payout  # caller commits, lock releases
```

The difference is one method call: `.with_for_update()`. Without it the code looks identical and tests green on a single connection. With it, the database serializes concurrent payout attempts at the row level, and the concurrency test in `tests/test_concurrency.py` proves it.

The AI generated syntactically correct, logically plausible, production-dangerous code. It was not a hallucination — it was a subtle omission of the one line that separates "works in staging" from "works under load."

---

## Architecture summary

```
POST /payouts/
      │
      ├─ Idempotency-Key header present?
      │       ├─ Yes → INSERT idempotency_keys (status=in_flight)
      │       │         ├─ IntegrityError → key exists
      │       │         │     ├─ completed → replay stored response
      │       │         │     └─ in_flight → 409 Conflict
      │       │         └─ Success → first request, proceed
      │       └─ No → proceed without idempotency tracking
      │
      ├─ BEGIN TRANSACTION
      ├─ SELECT * FROM merchants WHERE id=? FOR UPDATE   ← acquires lock
      ├─ SELECT SUM(credits) - SUM(debits) FROM ledger_entries
      ├─ balance >= amount? → No → raise InsufficientFunds → ROLLBACK
      ├─ INSERT payouts (status=PROCESSING)
      ├─ INSERT ledger_entries (type=DEBIT)
      ├─ UPDATE idempotency_keys (status=completed, response_body=...)
      └─ COMMIT   ← releases FOR UPDATE lock
```

## Running locally

```bash
docker-compose up -d db
pip install -r requirements.txt
DATABASE_URL=postgresql://ledger:ledger@localhost:5432/ledger \
  python -c "from app.database import engine, Base; from app.models.models import *; Base.metadata.create_all(engine)"
DATABASE_URL=postgresql://ledger:ledger@localhost:5432/ledger python scripts/seed.py
DATABASE_URL=postgresql://ledger:ledger@localhost:5432/ledger uvicorn app.main:app --reload
```

## Running tests

```bash
docker-compose up -d db_test
TEST_DATABASE_URL=postgresql://ledger:ledger@localhost:5433/ledger_test pytest tests/ -v
```
