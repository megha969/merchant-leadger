"""
Idempotency service.

How it works:
  1. Client sends a request with header Idempotency-Key: <key>
  2. We attempt to INSERT a row into idempotency_keys with status='in_flight'.
     - The UNIQUE constraint on (key, merchant_id) makes this atomic.
  3a. If the INSERT succeeds → this is the first time we've seen this key.
      Process the request, then UPDATE the row with the response and status='completed'.
  3b. If the INSERT fails with UniqueViolation → we've seen this key before.
      - If status='completed' → return the stored response immediately (replay).
      - If status='in_flight' → the original request is still processing.
        Return 409 Conflict — the client should retry after a short wait.

Why this is safe:
  - The INSERT is atomic due to the DB unique constraint; no application-level
    check-then-act race is possible.
  - In-flight detection prevents a second request from running duplicate logic
    while the first is still executing (e.g., within a payout, before commit).
  - We store the raw HTTP response so replays are byte-for-byte identical —
    the client cannot tell whether it got a fresh or replayed response.
"""
import json
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.models.models import IdempotencyKey


class IdempotencyConflict(Exception):
    """Raised when a key exists but is still in-flight."""
    pass


class IdempotencyReplay(Exception):
    """Raised when we have a completed response to replay."""
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self.body = body


def acquire_idempotency_key(
    db: Session,
    key: str,
    merchant_id: str,
    request_path: str,
) -> IdempotencyKey:
    """
    Attempt to claim this idempotency key.

    Returns the IdempotencyKey row if this is the first request.
    Raises IdempotencyReplay if we have a stored response to return.
    Raises IdempotencyConflict if the original request is still in-flight.
    """
    # Try to insert — the unique constraint enforces atomicity
    record = IdempotencyKey(
        key=key,
        merchant_id=merchant_id,
        request_path=request_path,
        status="in_flight",
    )
    db.add(record)
    try:
        db.flush()  # triggers constraint check without full commit
        return record
    except IntegrityError:
        db.rollback()

    # Row already exists — check its state
    existing = (
        db.query(IdempotencyKey)
        .filter(
            IdempotencyKey.key == key,
            IdempotencyKey.merchant_id == merchant_id,
        )
        .first()
    )

    if existing is None:
        # Shouldn't happen, but handle gracefully
        raise IdempotencyConflict("Idempotency key state unknown, retry.")

    if existing.status == "completed":
        # We have a stored response — replay it
        raise IdempotencyReplay(
            status_code=int(existing.response_status),
            body=json.loads(existing.response_body),
        )

    # status == 'in_flight' — original request still processing
    raise IdempotencyConflict(
        f"Request with idempotency key '{key}' is already in flight. "
        "Retry after a short delay."
    )


def complete_idempotency_key(
    db: Session,
    record: IdempotencyKey,
    status_code: int,
    response_body: dict,
) -> None:
    """
    Mark the idempotency key as completed and store the response for replay.
    Called after the business logic succeeds.
    """
    record.status = "completed"
    record.response_status = str(status_code)
    record.response_body = json.dumps(response_body)
    record.completed_at = datetime.utcnow()
