from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from app.database import get_db
from app.models.models import Merchant, LedgerEntry, TransactionType
from app.services.payout_service import get_balance

router = APIRouter()


class MerchantCreate(BaseModel):
    name: str
    email: str


class CreditRequest(BaseModel):
    amount: Decimal
    description: str = "Credit"
    reference_id: str = None


@router.post("/", status_code=201)
def create_merchant(body: MerchantCreate, db: Session = Depends(get_db)):
    existing = db.query(Merchant).filter(Merchant.email == body.email).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")
    merchant = Merchant(name=body.name, email=body.email)
    db.add(merchant)
    db.commit()
    db.refresh(merchant)
    return {"id": merchant.id, "name": merchant.name, "email": merchant.email}


@router.get("/")
def list_merchants(db: Session = Depends(get_db)):
    merchants = db.query(Merchant).all()
    return [
        {"id": m.id, "name": m.name, "email": m.email, "created_at": m.created_at}
        for m in merchants
    ]


@router.get("/{merchant_id}")
def get_merchant(merchant_id: str, db: Session = Depends(get_db)):
    merchant = db.query(Merchant).filter(Merchant.id == merchant_id).first()
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    return {"id": merchant.id, "name": merchant.name, "email": merchant.email}


@router.get("/{merchant_id}/balance")
def merchant_balance(merchant_id: str, db: Session = Depends(get_db)):
    merchant = db.query(Merchant).filter(Merchant.id == merchant_id).first()
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    balance = get_balance(db, merchant_id)
    return {"merchant_id": merchant_id, "balance": str(balance)}


@router.post("/{merchant_id}/credit", status_code=201)
def add_credit(merchant_id: str, body: CreditRequest, db: Session = Depends(get_db)):
    """Add a credit entry to a merchant's ledger (money in)."""
    merchant = db.query(Merchant).filter(Merchant.id == merchant_id).first()
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    if body.amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")

    entry = LedgerEntry(
        merchant_id=merchant_id,
        type=TransactionType.CREDIT,
        amount=body.amount,
        description=body.description,
        reference_id=body.reference_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)

    balance = get_balance(db, merchant_id)
    return {
        "entry_id": entry.id,
        "merchant_id": merchant_id,
        "type": "credit",
        "amount": str(entry.amount),
        "balance": str(balance),
    }


@router.get("/{merchant_id}/ledger")
def merchant_ledger(merchant_id: str, db: Session = Depends(get_db)):
    merchant = db.query(Merchant).filter(Merchant.id == merchant_id).first()
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    entries = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.merchant_id == merchant_id)
        .order_by(LedgerEntry.created_at.desc())
        .limit(100)
        .all()
    )
    balance = get_balance(db, merchant_id)
    return {
        "merchant_id": merchant_id,
        "balance": str(balance),
        "entries": [
            {
                "id": e.id,
                "type": e.type,
                "amount": str(e.amount),
                "description": e.description,
                "reference_id": e.reference_id,
                "created_at": e.created_at,
            }
            for e in entries
        ],
    }
