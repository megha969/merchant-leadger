# Merchant Ledger API

A concurrency-safe merchant payout ledger with idempotent request handling.

**Live demo:** https://merchant-ledger.onrender.com  
**API docs:** https://merchant-ledger.onrender.com/docs  
**EXPLAINER.md:** [EXPLAINER.md](./EXPLAINER.md)

---

## Quick start (Docker)

```bash
git clone https://github.com/YOUR_USERNAME/merchant-ledger
cd merchant-ledger
docker-compose up
```

API is at `http://localhost:8000`. Docs at `http://localhost:8000/docs`.

---

## Manual setup

### Prerequisites
- Python 3.11+
- PostgreSQL 14+

### Install

```bash
pip install -r requirements.txt
```

### Configure

```bash
export DATABASE_URL=postgresql://ledger:ledger@localhost:5432/ledger
```

### Create schema

```bash
python -c "
from app.database import engine, Base
from app.models.models import *
Base.metadata.create_all(engine)
"
```

### Seed test data

```bash
python scripts/seed.py
```

### Run

```bash
uvicorn app.main:app --reload
```

---

## Running tests

Tests require PostgreSQL (SQLite doesn't support `SELECT FOR UPDATE`).

```bash
# Start test DB
docker-compose up -d db_test

# Run all tests
TEST_DATABASE_URL=postgresql://ledger:ledger@localhost:5433/ledger_test pytest tests/ -v

# Run specific test files
pytest tests/test_concurrency.py -v
pytest tests/test_idempotency.py -v
```

---

## Key endpoints

```
GET  /                              Health / info
GET  /docs                          Interactive API docs

POST /merchants/                    Create merchant
GET  /merchants/                    List merchants
GET  /merchants/{id}/balance        Get balance
POST /merchants/{id}/credit         Add credit (money in)
GET  /merchants/{id}/ledger         Full ledger history

POST /payouts/                      Request payout (supports Idempotency-Key header)
GET  /payouts/                      List payouts
GET  /payouts/{id}                  Get payout
PATCH /payouts/{id}/status          Transition payout status

GET  /transactions/                 List ledger entries
```

### Example: payout with idempotency

```bash
curl -X POST https://merchant-ledger.onrender.com/payouts/ \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"merchant_id": "MERCHANT_ID", "amount": "50.00"}'
```

Repeat the same `Idempotency-Key` — you'll get the same `payout_id` back with no double-charge.

---

## Design notes

See [EXPLAINER.md](./EXPLAINER.md) for detailed explanations of:
- The ledger model and balance query
- The `SELECT FOR UPDATE` concurrency lock
- The idempotency key protocol (including in-flight handling)
- The payout state machine
- An honest AI audit (what the AI got wrong, what I caught)
