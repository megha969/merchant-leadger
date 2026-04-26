#!/usr/bin/env python3
"""
Seed script: populates the database with realistic test merchants and transactions.

Usage:
    python scripts/seed.py

Requires DATABASE_URL env var or defaults to localhost.
"""
import os
import sys
import uuid
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models.models import Merchant, LedgerEntry, Payout, TransactionType, PayoutStatus

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://ledger:ledger@localhost:5432/ledger")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)


MERCHANTS = [
    {
        "name": "Acme Coffee Roasters",
        "email": "accounting@acmecoffee.com",
        "credits": [
            ("1200.00", "Payment from customer ORD-001"),
            ("850.00", "Payment from customer ORD-002"),
            ("340.00", "Payment from customer ORD-003"),
        ],
        "payouts": [
            ("500.00", PayoutStatus.COMPLETED),
            ("300.00", PayoutStatus.COMPLETED),
        ],
    },
    {
        "name": "Pixel Perfect Studio",
        "email": "finance@pixelperfect.io",
        "credits": [
            ("5000.00", "Invoice INV-2024-001"),
            ("2500.00", "Invoice INV-2024-002"),
        ],
        "payouts": [
            ("3000.00", PayoutStatus.COMPLETED),
            ("1000.00", PayoutStatus.FAILED),
        ],
    },
    {
        "name": "Harbor Books",
        "email": "books@harborbooks.com",
        "credits": [
            ("450.00", "Online sales batch March W1"),
            ("620.00", "Online sales batch March W2"),
            ("380.00", "Marketplace settlement"),
        ],
        "payouts": [
            ("200.00", PayoutStatus.COMPLETED),
        ],
    },
    {
        "name": "Nomad Gear Co",
        "email": "hello@nomadgear.com",
        "credits": [
            ("9900.00", "Shopify payout batch"),
        ],
        "payouts": [
            ("5000.00", PayoutStatus.PROCESSING),
        ],
    },
    {
        "name": "Fresh Table",
        "email": "ops@freshtable.co",
        "credits": [
            ("200.00", "Week 12 settlement"),
        ],
        "payouts": [],  # No payouts yet — balance sitting
    },
]


def seed():
    Base.metadata.create_all(bind=engine)
    session = Session()

    print("Seeding merchants...")

    for data in MERCHANTS:
        # Skip if already exists
        existing = session.query(Merchant).filter(Merchant.email == data["email"]).first()
        if existing:
            print(f"  Skipping {data['name']} (already exists)")
            continue

        merchant = Merchant(name=data["name"], email=data["email"])
        session.add(merchant)
        session.flush()
        print(f"  Created merchant: {data['name']} ({merchant.id})")

        total_credits = Decimal("0")
        total_debits = Decimal("0")

        # Add credits
        for amount_str, description in data["credits"]:
            amount = Decimal(amount_str)
            entry = LedgerEntry(
                merchant_id=merchant.id,
                type=TransactionType.CREDIT,
                amount=amount,
                description=description,
                reference_id=f"REF-{uuid.uuid4().hex[:8].upper()}",
            )
            session.add(entry)
            total_credits += amount

        session.flush()

        # Add payouts
        for amount_str, status in data["payouts"]:
            amount = Decimal(amount_str)
            payout = Payout(
                merchant_id=merchant.id,
                amount=amount,
                status=status,
            )
            session.add(payout)
            session.flush()

            # Debit entry
            debit = LedgerEntry(
                merchant_id=merchant.id,
                type=TransactionType.DEBIT,
                amount=amount,
                description=f"Payout ({status})",
                reference_id=payout.id,
                payout_id=payout.id,
            )
            session.add(debit)
            total_debits += amount

            # If failed, add compensating credit
            if status == PayoutStatus.FAILED:
                reversal = LedgerEntry(
                    merchant_id=merchant.id,
                    type=TransactionType.CREDIT,
                    amount=amount,
                    description="Payout reversal (seed)",
                    reference_id=payout.id,
                    payout_id=payout.id,
                )
                session.add(reversal)
                total_credits += amount

                payout.failure_reason = "Seeded failure for testing"

        expected_balance = total_credits - total_debits
        print(f"    Credits: {total_credits}, Debits: {total_debits}, Balance: {expected_balance}")

    session.commit()
    session.close()
    print("\nSeed complete.")


if __name__ == "__main__":
    seed()
