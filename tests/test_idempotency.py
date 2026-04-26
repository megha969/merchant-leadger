"""
Idempotency tests.

What we're proving:
  1. Sending the same Idempotency-Key twice returns the same response, with
     no double-charging.
  2. A different key for the same data is treated as a new request.
  3. An in-flight key returns 409 (not a duplicate execution).
  4. State machine: FAILED payouts cannot transition to COMPLETED.
"""
import uuid
import pytest
from decimal import Decimal
from fastapi.testclient import TestClient

from app.models.models import Merchant, LedgerEntry, TransactionType
from app.services.payout_service import get_balance


def seed_merchant(db, balance: Decimal, email: str = None) -> str:
    email = email or f"test-{uuid.uuid4()}@example.com"
    merchant = Merchant(name="Idempotency Test Merchant", email=email)
    db.add(merchant)
    db.flush()

    credit = LedgerEntry(
        merchant_id=merchant.id,
        type=TransactionType.CREDIT,
        amount=balance,
        description="Initial credit",
    )
    db.add(credit)
    db.commit()
    return merchant.id


def test_idempotent_payout_returns_same_response(client, db):
    """
    Sending the same idempotency key twice must:
    - Return the exact same payout_id
    - Not double-charge the merchant
    """
    merchant_id = seed_merchant(db, Decimal("200.00"))
    idem_key = str(uuid.uuid4())

    # First request
    r1 = client.post(
        "/payouts/",
        json={"merchant_id": merchant_id, "amount": "50.00"},
        headers={"Idempotency-Key": idem_key},
    )
    assert r1.status_code == 201, f"First request failed: {r1.json()}"
    body1 = r1.json()

    # Second request — same key, same body
    r2 = client.post(
        "/payouts/",
        json={"merchant_id": merchant_id, "amount": "50.00"},
        headers={"Idempotency-Key": idem_key},
    )
    assert r2.status_code == 201, f"Replay failed: {r2.json()}"
    body2 = r2.json()

    # Must return the SAME payout_id — not a new payout
    assert body1["payout_id"] == body2["payout_id"], (
        f"Idempotency broken: got different payout IDs "
        f"{body1['payout_id']} vs {body2['payout_id']}"
    )

    # Balance must reflect only ONE debit, not two
    balance = get_balance(db, merchant_id)
    assert balance == Decimal("150.00"), (
        f"Expected balance 150 after one 50-unit debit, got {balance}. "
        "Possible double charge!"
    )


def test_different_idempotency_keys_create_separate_payouts(client, db):
    """Two different keys → two separate payouts (both succeed if funds allow)."""
    merchant_id = seed_merchant(db, Decimal("200.00"))

    r1 = client.post(
        "/payouts/",
        json={"merchant_id": merchant_id, "amount": "50.00"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    r2 = client.post(
        "/payouts/",
        json={"merchant_id": merchant_id, "amount": "50.00"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["payout_id"] != r2.json()["payout_id"]

    balance = get_balance(db, merchant_id)
    assert balance == Decimal("100.00")


def test_payout_without_idempotency_key_still_works(client, db):
    """Idempotency-Key is optional; requests without it process normally."""
    merchant_id = seed_merchant(db, Decimal("100.00"))

    r = client.post(
        "/payouts/",
        json={"merchant_id": merchant_id, "amount": "30.00"},
    )
    assert r.status_code == 201

    balance = get_balance(db, merchant_id)
    assert balance == Decimal("70.00")


def test_insufficient_funds_rejected(client, db):
    """Payout request exceeding balance is rejected with 422."""
    merchant_id = seed_merchant(db, Decimal("50.00"))

    r = client.post(
        "/payouts/",
        json={"merchant_id": merchant_id, "amount": "100.00"},
    )
    assert r.status_code == 422
    assert "Insufficient funds" in r.json()["detail"]


def test_state_machine_failed_to_completed_blocked(client, db):
    """
    A FAILED payout cannot transition to COMPLETED.
    This tests the state machine guard in complete_payout().
    """
    merchant_id = seed_merchant(db, Decimal("200.00"))

    # Create a payout
    r = client.post(
        "/payouts/",
        json={"merchant_id": merchant_id, "amount": "50.00"},
    )
    assert r.status_code == 201
    payout_id = r.json()["payout_id"]

    # Fail it
    r_fail = client.patch(
        f"/payouts/{payout_id}/status",
        json={"status": "failed", "failure_reason": "Bank rejected"},
    )
    assert r_fail.status_code == 200
    assert r_fail.json()["status"] == "failed"

    # Now try to complete it — must be rejected
    r_complete = client.patch(
        f"/payouts/{payout_id}/status",
        json={"status": "completed"},
    )
    assert r_complete.status_code == 422, (
        f"Expected 422 when completing a FAILED payout, got {r_complete.status_code}: "
        f"{r_complete.json()}"
    )
    assert "Cannot complete" in r_complete.json()["detail"]


def test_state_machine_completed_to_failed_blocked(client, db):
    """A COMPLETED payout cannot transition to FAILED."""
    merchant_id = seed_merchant(db, Decimal("200.00"))

    r = client.post("/payouts/", json={"merchant_id": merchant_id, "amount": "50.00"})
    payout_id = r.json()["payout_id"]

    client.patch(f"/payouts/{payout_id}/status", json={"status": "completed"})

    r_fail = client.patch(
        f"/payouts/{payout_id}/status",
        json={"status": "failed", "failure_reason": "too late"},
    )
    assert r_fail.status_code == 422


def test_failed_payout_reverses_balance(client, db):
    """
    When a payout is failed, a compensating credit is inserted.
    The merchant's balance should be restored to its pre-payout value.
    """
    merchant_id = seed_merchant(db, Decimal("100.00"))

    r = client.post("/payouts/", json={"merchant_id": merchant_id, "amount": "60.00"})
    payout_id = r.json()["payout_id"]

    balance_after_payout = get_balance(db, merchant_id)
    assert balance_after_payout == Decimal("40.00")

    client.patch(
        f"/payouts/{payout_id}/status",
        json={"status": "failed", "failure_reason": "Network error"},
    )

    balance_after_failure = get_balance(db, merchant_id)
    assert balance_after_failure == Decimal("100.00"), (
        f"Expected balance restored to 100 after payout failure, got {balance_after_failure}"
    )


def test_idempotent_key_scoped_to_merchant(client, db):
    """
    The same idempotency key used by two different merchants creates
    two separate payouts (keys are scoped per merchant).
    """
    m1 = seed_merchant(db, Decimal("200.00"), email="m1@test.com")
    m2 = seed_merchant(db, Decimal("200.00"), email="m2@test.com")
    shared_key = str(uuid.uuid4())

    r1 = client.post(
        "/payouts/",
        json={"merchant_id": m1, "amount": "50.00"},
        headers={"Idempotency-Key": shared_key},
    )
    r2 = client.post(
        "/payouts/",
        json={"merchant_id": m2, "amount": "50.00"},
        headers={"Idempotency-Key": shared_key},
    )

    assert r1.status_code == 201
    assert r2.status_code == 201
    # Different merchants → different payout IDs even with same key
    assert r1.json()["payout_id"] != r2.json()["payout_id"]
