from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import LedgerEntry

router = APIRouter()


@router.get("/")
def list_transactions(
    merchant_id: str = None,
    entry_type: str = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    q = db.query(LedgerEntry)
    if merchant_id:
        q = q.filter(LedgerEntry.merchant_id == merchant_id)
    if entry_type:
        q = q.filter(LedgerEntry.type == entry_type)
    entries = q.order_by(LedgerEntry.created_at.desc()).limit(min(limit, 200)).all()
    return [
        {
            "id": e.id,
            "merchant_id": e.merchant_id,
            "type": e.type,
            "amount": str(e.amount),
            "description": e.description,
            "reference_id": e.reference_id,
            "payout_id": e.payout_id,
            "created_at": e.created_at,
        }
        for e in entries
    ]


@router.get("/{entry_id}")
def get_transaction(entry_id: str, db: Session = Depends(get_db)):
    entry = db.query(LedgerEntry).filter(LedgerEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return {
        "id": entry.id,
        "merchant_id": entry.merchant_id,
        "type": entry.type,
        "amount": str(entry.amount),
        "description": entry.description,
        "reference_id": entry.reference_id,
        "payout_id": entry.payout_id,
        "created_at": entry.created_at,
    }
