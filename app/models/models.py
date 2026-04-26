import uuid
import enum
from datetime import datetime
from sqlalchemy import (
    Column, String, Numeric, DateTime, ForeignKey,
    Enum as SAEnum, Index, UniqueConstraint, Text, CheckConstraint
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


def new_uuid():
    return str(uuid.uuid4())


class TransactionType(str, enum.Enum):
    CREDIT = "credit"   # money in (e.g. payment received)
    DEBIT = "debit"     # money out (e.g. payout, refund)


class PayoutStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Merchant(Base):
    __tablename__ = "merchants"

    id = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    ledger_entries = relationship("LedgerEntry", back_populates="merchant")
    payouts = relationship("Payout", back_populates="merchant")


class LedgerEntry(Base):
    """
    Immutable append-only ledger.

    Every credit or debit is a new row — we never UPDATE amounts.
    Balance = SUM(amount WHERE type=credit) - SUM(amount WHERE type=debit)

    Amounts are always positive. Type determines direction.
    """
    __tablename__ = "ledger_entries"

    id = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    merchant_id = Column(UUID(as_uuid=False), ForeignKey("merchants.id"), nullable=False)
    type = Column(SAEnum(TransactionType), nullable=False)
    amount = Column(Numeric(18, 2), nullable=False)
    description = Column(Text, nullable=True)
    reference_id = Column(String(255), nullable=True)   # external payment ID, payout ID, etc.
    payout_id = Column(UUID(as_uuid=False), ForeignKey("payouts.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("amount > 0", name="positive_amount"),
        Index("ix_ledger_merchant_created", "merchant_id", "created_at"),
    )

    merchant = relationship("Merchant", back_populates="ledger_entries")
    payout = relationship("Payout", back_populates="ledger_entries")


class IdempotencyKey(Base):
    """
    Stores the response for idempotent requests.

    States:
      - in_flight: request is being processed (locked row, no response yet)
      - completed: request finished; response_body has the result
    """
    __tablename__ = "idempotency_keys"

    id = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    key = Column(String(255), nullable=False)
    merchant_id = Column(UUID(as_uuid=False), ForeignKey("merchants.id"), nullable=False)
    status = Column(String(50), nullable=False, default="in_flight")  # in_flight | completed
    request_path = Column(String(500), nullable=False)
    response_status = Column(String(10), nullable=True)
    response_body = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("key", "merchant_id", name="uq_idempotency_key_merchant"),
    )


class Payout(Base):
    """
    Payout state machine:

        PENDING → PROCESSING → COMPLETED
                         ↘ FAILED

    Only PENDING → PROCESSING is allowed to proceed.
    COMPLETED and FAILED are terminal states.
    """
    __tablename__ = "payouts"

    id = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    merchant_id = Column(UUID(as_uuid=False), ForeignKey("merchants.id"), nullable=False)
    amount = Column(Numeric(18, 2), nullable=False)
    status = Column(SAEnum(PayoutStatus), nullable=False, default=PayoutStatus.PENDING)
    idempotency_key_id = Column(UUID(as_uuid=False), ForeignKey("idempotency_keys.id"), nullable=True)
    failure_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("amount > 0", name="payout_positive_amount"),
    )

    merchant = relationship("Merchant", back_populates="payouts")
    ledger_entries = relationship("LedgerEntry", back_populates="payout")
