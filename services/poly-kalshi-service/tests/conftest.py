from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from poly_kalshi_service.config import Settings
from poly_kalshi_service.decision import StrategyParams
from poly_kalshi_service.engine import Engine
from poly_kalshi_service.ledger import Ledger
from poly_kalshi_service.risk import RiskLimits, RiskState
from poly_kalshi_service.trades import JsonlDaily
from poly_kalshi_service.venue import PaperVenue
from poly_kalshi_service.windows import Window

API_KEY = "test-api-key-at-least-16"
AUTH = {"X-API-Key": API_KEY}
NS = 1_000_000_000
OPEN = 1_791_297_900  # a window open (multiple of 900)

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
PUBLIC_KEY = _KEY.public_key()


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    key = tmp_path / "kalshi.pem"
    key.write_bytes(PEM)
    values: dict[str, Any] = {
        "API_KEY": API_KEY,
        "KALSHI_API_KEY_ID": "test-key-id",
        "KALSHI_PRIVATE_KEY_PATH": str(key),
        "PK_MAX_OPEN_NOTIONAL_USD": 500,
        "PK_DAILY_LOSS_LIMIT_USD": 250,
        "PK_RECORD_DIR": str(tmp_path / "raw"),
        "PK_STATE_DIR": str(tmp_path / "state"),
        "PK_KILL_FILE_PATH": str(tmp_path / "KILL"),
        "EVENTS_LOG_PATH": str(tmp_path / "events.jsonl"),
        "PK_HOST_TAG": "test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def window(asset: str = "BTC", open_unix: int = OPEN, **overrides: Any) -> Window:
    values: dict[str, Any] = {
        "asset": asset,
        "open_unix": open_unix,
        "kalshi_ticker": f"KX{asset}15M-{open_unix}",
        "yes_token": f"{asset}-yes-{open_unix}",
        "no_token": f"{asset}-no-{open_unix}",
        "condition_id": f"0x{asset}{open_unix}",
    }
    values.update(overrides)
    return Window(**values)


def at(open_unix: int, sec: float) -> int:
    return int((open_unix + sec) * NS)


def ctl(body: dict[str, Any]) -> str:
    return json.dumps(body)


def kalshi_snapshot(
    ticker: str,
    yes: list[tuple[float, float]],
    no: list[tuple[float, float]],
    seq: int = 1,
    sid: int = 1,
) -> str:
    return json.dumps(
        {
            "type": "orderbook_snapshot",
            "sid": sid,
            "seq": seq,
            "msg": {
                "market_ticker": ticker,
                "yes_dollars_fp": [[f"{p:.4f}", f"{s:.2f}"] for p, s in yes],
                "no_dollars_fp": [[f"{p:.4f}", f"{s:.2f}"] for p, s in no],
            },
        }
    )


def kalshi_delta(
    ticker: str, side: str, price: float, delta: float, seq: int = 2, sid: int = 1
) -> str:
    return json.dumps(
        {
            "type": "orderbook_delta",
            "sid": sid,
            "seq": seq,
            "msg": {
                "market_ticker": ticker,
                "price_dollars": f"{price:.4f}",
                "delta_fp": f"{delta:.2f}",
                "side": side,
            },
        }
    )


def poly_book(token: str, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> str:
    return json.dumps(
        [
            {
                "event_type": "book",
                "asset_id": token,
                "market": "0x",
                "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
                "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
            }
        ]
    )


def poly_change(token: str, price: float, size: float, side: str, **extra: Any) -> str:
    change = {"asset_id": token, "price": str(price), "size": str(size), "side": side, **extra}
    return json.dumps({"event_type": "price_change", "market": "0x", "price_changes": [change]})


def make_engine(
    tmp_path: Path | None = None,
    *,
    params: StrategyParams | None = None,
    assets: tuple[str, ...] = ("BTC",),
    latency_ms: int = 150,
    cash: float = 1000.0,
    paper: bool = True,
    max_open: float = 500.0,
    kill_file: Path | None = None,
) -> Engine:
    return Engine(
        params or StrategyParams(),
        assets,
        ledger=Ledger.open(tmp_path / "ledger.json" if tmp_path else None, cash),
        risk=RiskState(
            RiskLimits(
                max_open_notional_usd=max_open, daily_loss_limit_usd=250, kill_file=kill_file
            )
        ),
        venue=PaperVenue(latency_ms) if paper else None,
        journal=JsonlDaily(tmp_path / "journal" if tmp_path else None),
    )


def setup_window(engine: Engine, w: Window, *, sec: float = 0.5) -> int:
    """Register ``w`` and give all three legs a book at ``sec``.

    Kalshi YES 0.70 / 0.72 (mid 0.71); Polymarket YES 0.44 / 0.46 (mid 0.45),
    NO 0.54 / 0.56. Gap +0.26: buy YES at 0.46.
    """
    t = at(w.open_unix, sec)
    engine.handle_line(t, "c", ctl(w.control()))
    engine.handle_line(t + 1, "k", kalshi_snapshot(w.kalshi_ticker, [(0.70, 500)], [(0.28, 500)]))
    engine.handle_line(t + 2, "p", poly_book(w.yes_token, [(0.44, 300)], [(0.46, 60), (0.47, 80)]))
    engine.handle_line(t + 3, "p", poly_book(w.no_token, [(0.54, 300)], [(0.56, 300)]))
    return t + 3


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return make_engine(tmp_path)
