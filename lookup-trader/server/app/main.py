from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.bootstrap import bootstrap
from app.routers import candles, compare, sessions, setups, trades


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap()
    yield


app = FastAPI(title="Lookup Trader API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(candles.router)
app.include_router(setups.router)
app.include_router(sessions.router)
app.include_router(trades.router)
app.include_router(compare.router)


@app.get("/health")
def health():
    return {"status": "ok"}
