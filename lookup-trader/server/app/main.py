from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.bootstrap import bootstrap
from app.db.duck import close_all
from app.routers import (
    base_rate,
    calendar,
    candles,
    compare,
    context,
    export,
    meta_events,
    meta_model,
    outcome,
    screenshots,
    sessions,
    setups,
    signals,
    trades,
)
from app.routers import health as health_router
from app.services.market_execution import ExecutionConfig


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The API never places orders, but it shares deployment settings and health
    # surfaces with the worker. Reject an enabled-but-ambiguous provider config
    # at process startup rather than reporting a deceptively healthy API.
    ExecutionConfig.from_settings(settings)
    bootstrap()
    try:
        yield
    finally:
        # DuckDB is single-writer, and the instance cached in app.db.duck holds
        # an exclusive lock on engine.duckdb for the life of the process. Drop
        # it deterministically on shutdown rather than leaving it to the OS:
        # if any instance lingers, every subsequent start fails to acquire the
        # lock, which turns one bad exit into a permanent restart loop.
        close_all()


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
app.include_router(signals.router)
app.include_router(compare.router)
app.include_router(screenshots.router)
app.include_router(export.router)
app.include_router(context.router)
app.include_router(base_rate.router)
app.include_router(outcome.router)
app.include_router(meta_events.router)
app.include_router(meta_model.router)
app.include_router(calendar.router)
app.include_router(health_router.router)


@app.get("/health")
def health():
    return health_router.get_data_model_health()
