"""
Concurrency test: two simultaneous payout requests for the same merchant.

What we're proving:
  Given a merchant with balance = 100,
  when two threads simultaneously request a payout of 80,
  exactly ONE should succeed and ONE should fail with InsufficientFunds.

  Without SELECT FOR UPDATE, both threads could read balance=100,
  both decide they can pay out 80, and both succeed — leaving balance at -60.

  With SELECT FOR UPDATE, the second thread blocks until the first commits,
  then sees the updated balance of 20 and correctly rejects the payout.

This test REQUIRES PostgreSQL. It intentionally creates two separate DB
connections to simulate two independent HTTP requests hitting the server
simultaneously.
"""
import threading
import pytest
from decimal import Decimal
from sqlalchemy.orm import Session

from app.models.models import Merchant, LedgerEntry, TransactionType
from app.services.payout_service import create_payout, InsufficientFundsError


def seed_merchant_with_balance(session: Session, balance: Decimal) -> str:
    """Create a merchant and credit them the given balance."""
    merchant = Merchant(name="Test Merchant", email="concurrent@test.com")
    session.add(merchant)
    session.flush()

    credit = LedgerEntry(
        merchant_id=merchant.id,
        type=TransactionType.CREDIT,
        amount=balance,
        description="Initial credit",
    )
    session.add(credit)
    session.commit()
    return merchant.id


class PayoutResult:
    def __init__(self):
        self.success = False
        self.error = None
        self.payout_id = None


def attempt_payout(make_session, merchant_id: str, amount: Decimal, result: PayoutResult, barrier: threading.Barrier):
    """
    Runs in a thread. Uses its own DB session (its own connection).
    The barrier ensures both threads start the payout logic at the same time.
    """
    session = make_session()
    try:
        barrier.wait()  # both threads start simultaneously
        payout = create_payout(session, merchant_id, amount)
        session.commit()
        result.success = True
        result.payout_id = payout.id
    except InsufficientFundsError as e:
        result.success = False
        result.error = str(e)
        session.rollback()
    except Exception as e:
        result.success = False
        result.error = f"Unexpected: {e}"
        session.rollback()
    finally:
        session.close()


@pytest.mark.usefixtures()
def test_concurrent_payouts_only_one_succeeds(fresh_client):
    """
    Two threads race to payout 80 from a balance of 100.
    Exactly one must succeed; exactly one must fail.
    The balance after must be exactly 20 (not -60).
    """
    make_session, engine = fresh_client

    # Seed: merchant with balance 100
    setup_session = make_session()
    merchant_id = seed_merchant_with_balance(setup_session, Decimal("100.00"))
    setup_session.close()

    payout_amount = Decimal("80.00")
    result1 = PayoutResult()
    result2 = PayoutResult()

    # Barrier ensures both threads reach attempt_payout at the same time
    barrier = threading.Barrier(2, timeout=10)

    t1 = threading.Thread(
        target=attempt_payout,
        args=(make_session, merchant_id, payout_amount, result1, barrier),
    )
    t2 = threading.Thread(
        target=attempt_payout,
        args=(make_session, merchant_id, payout_amount, result2, barrier),
    )

    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    successes = [r for r in [result1, result2] if r.success]
    failures = [r for r in [result1, result2] if not r.success]

    # Core assertion: exactly one payout went through
    assert len(successes) == 1, (
        f"Expected exactly 1 success, got {len(successes)}. "
        f"Results: success={result1.success}/{result2.success}, "
        f"errors={result1.error}/{result2.error}"
    )
    assert len(failures) == 1, f"Expected exactly 1 failure, got {len(failures)}"

    # The failure must be InsufficientFunds, not a deadlock or crash
    assert "Insufficient funds" in failures[0].error, (
        f"Expected InsufficientFundsError, got: {failures[0].error}"
    )

    # Verify the ledger balance is correct: 100 - 80 = 20
    from app.services.payout_service import get_balance
    check_session = make_session()
    balance = get_balance(check_session, merchant_id)
    check_session.close()

    assert balance == Decimal("20.00"), (
        f"Expected balance 20.00 after one payout of 80, got {balance}"
    )


def test_concurrent_payouts_both_within_balance(fresh_client):
    """
    Two payouts that individually fit but together would not.
    This is the classic race: balance=100, payout1=60, payout2=60.
    Only one should succeed.
    """
    make_session, engine = fresh_client

    setup_session = make_session()
    merchant_id = seed_merchant_with_balance(setup_session, Decimal("100.00"))
    setup_session.close()

    result1 = PayoutResult()
    result2 = PayoutResult()
    barrier = threading.Barrier(2, timeout=10)

    t1 = threading.Thread(
        target=attempt_payout,
        args=(make_session, merchant_id, Decimal("60.00"), result1, barrier),
    )
    t2 = threading.Thread(
        target=attempt_payout,
        args=(make_session, merchant_id, Decimal("60.00"), result2, barrier),
    )

    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    successes = [r for r in [result1, result2] if r.success]
    assert len(successes) == 1, (
        f"Both payouts of 60 from balance 100 succeeded — race condition! "
        f"Errors: {result1.error} / {result2.error}"
    )
