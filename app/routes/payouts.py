from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from app.database import get_db
from app.models.models import Merchant, Payout, PayoutStatus
from app.services.payout_service import (
    create_payout, complete_payout, fail_payout,
    InsufficientFundsError, InvalidStateTransitionError, MerchantNotFoundError
)
from app.services.idempotency_service import (
    acquire_idempotency_key, complete_idempotency_key,
    IdempotencyConflict, IdempotencyReplay
)

router = APIRouter()


class PayoutRequest(BaseModel):
    merchant_id: str
    amount: Decimal


class PayoutStatusUpdate(BaseModel):
    status: str
    failure_reason: Optional[str] = None


@router.post("/", status_code=201)
def request_payout(
    body: PayoutRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    """
    Initiate a payout for a merchant.

    Idempotency:
      Pass `Idempotency-Key: <uuid>` header to make this request safe to retry.
      Repeating the same key returns the original response without double-charging.

    Concurrency:
      Uses SELECT FOR UPDATE on the merchant row to serialize concurrent payout
      attempts. Two simultaneous requests for the same merchant will queue —
      the second will see the updated balance after the first commits.
    """
    # Validate merchant exists before acquiring idempotency key
    merchant = db.query(Merchant).filter(Merchant.id == body.merchant_id).first()
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    # --- IDEMPOTENCY HANDLING ---
    idem_record = None
    if idempotency_key:
        try:
            idem_record = acquire_idempotency_key(
                db,
                key=idempotency_key,
                merchant_id=body.merchant_id,
                request_path="/payouts",
            )
        except IdempotencyReplay as replay:
            # We've seen this key and have a stored response — return it as-is
            return JSONResponse(status_code=replay.status_code, content=replay.body)
        except IdempotencyConflict as conflict:
            raise HTTPException(status_code=409, detail=str(conflict))

    # --- BUSINESS LOGIC (runs inside a single transaction) ---
    try:
        payout = create_payout(db, body.merchant_id, body.amount)

        response_body = {
            "payout_id": payout.id,
            "merchant_id": body.merchant_id,
            "amount": str(payout.amount),
            "status": payout.status,
        }

        if idem_record:
            complete_idempotency_key(db, idem_record, 201, response_body)

        db.commit()
        return JSONResponse(status_code=201, content=response_body)

    except InsufficientFundsError as e:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(e))
    except MerchantNotFoundError as e:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        db.rollback()
        raise


@router.get("/")
def list_payouts(merchant_id: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(Payout)
    if merchant_id:
        q = q.filter(Payout.merchant_id == merchant_id)
    payouts = q.order_by(Payout.created_at.desc()).limit(100).all()
    return [
        {
            "id": p.id,
            "merchant_id": p.merchant_id,
            "amount": str(p.amount),
            "status": p.status,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        }
        for p in payouts
    ]


@router.get("/{payout_id}")
def get_payout(payout_id: str, db: Session = Depends(get_db)):
    payout = db.query(Payout).filter(Payout.id == payout_id).first()
    if not payout:
        raise HTTPException(status_code=404, detail="Payout not found")
    return {
        "id": payout.id,
        "merchant_id": payout.merchant_id,
        "amount": str(payout.amount),
        "status": payout.status,
        "failure_reason": payout.failure_reason,
        "created_at": payout.created_at,
        "updated_at": payout.updated_at,
    }


@router.patch("/{payout_id}/status")
def update_payout_status(
    payout_id: str,
    body: PayoutStatusUpdate,
    db: Session = Depends(get_db),
):
    """
    Transition payout status. Enforces the state machine:
      PROCESSING → COMPLETED
      PROCESSING → FAILED

    COMPLETED and FAILED are terminal — any other transition raises 422.
    """
    try:
        if body.status == "completed":
            payout = complete_payout(db, payout_id)
        elif body.status == "failed":
            reason = body.failure_reason or "Unknown failure"
            payout = fail_payout(db, payout_id, reason)
        else:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid status '{body.status}'. Must be 'completed' or 'failed'.",
            )
        db.commit()
        return {
            "id": payout.id,
            "status": payout.status,
            "failure_reason": payout.failure_reason,
            "updated_at": payout.updated_at,
        }
    except InvalidStateTransitionError as e:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(e))
