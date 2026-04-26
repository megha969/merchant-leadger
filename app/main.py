from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes import merchants, transactions, payouts
from app.database import engine, Base

app = FastAPI(
    title="Merchant Ledger API",
    description="Double-entry ledger with concurrency-safe payouts",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(merchants.router, prefix="/merchants", tags=["merchants"])
app.include_router(transactions.router, prefix="/transactions", tags=["transactions"])
app.include_router(payouts.router, prefix="/payouts", tags=["payouts"])


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {
        "service": "Merchant Ledger API",
        "docs": "/docs",
        "health": "/health",
    }
