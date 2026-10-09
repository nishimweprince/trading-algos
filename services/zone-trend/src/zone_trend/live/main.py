"""Process wiring for ``zt shadow``, ``zt live``, ``zt kill`` and ``zt parity``.

The Binance plugin is discovered by entry point (``ta_plugin_api``), never
constructed here. Shadow mode reads mainnet public data through the
market-data factory's client and sends nothing; live mode signs with the
trading key on the environment's order host and starts the user stream.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx
import pandas as pd
from ta_plugin_api import EXECUTION_GROUP, MARKET_DATA_GROUP, load_providers

from zone_trend.config import load_config
from zone_trend.core.replay import replay
from zone_trend.data.store import DataPaths, load
from zone_trend.live.notify import AlertSink
from zone_trend.live.oms import Oms
from zone_trend.live.reconciler import Reconciler
from zone_trend.live.runner import Runner, acquire_lock, kill
from zone_trend.live.settings import LiveSettings
from zone_trend.live.venues import LiveVenue, PaperVenue
from zone_trend.persistence.repo import Repo

__all__ = ["parity", "run_kill", "run_service"]

log = logging.getLogger(__name__)
PROVIDER = "binance_futures"


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


def _symbols(settings: LiveSettings, cfg) -> list[str]:
    if settings.zt_symbols:
        return settings.zt_symbols
    paths = DataPaths.from_env(settings.zt_data_dir)
    hist = paths.root / "universe" / "history.json"
    if hist.exists():
        import json

        months = json.loads(hist.read_text())["months"]
        latest = sorted(months)[-1]
        return months[latest][: cfg.universe.size]
    raise SystemExit("set ZT_SYMBOLS or build the universe history (zt data universe)")


def _repo(settings: LiveSettings, cfg) -> Repo:
    url = settings.zt_db_url or cfg.live.db_url
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return Repo(url)


async def _clock(trading, settings: LiveSettings, strict: bool):
    reply = await trading.server_time()
    if not reply.ok:
        raise SystemExit(f"cannot read Binance server time: {reply.outcome}")
    offset = int(reply.body["serverTime"]) - _wall_ms()
    if abs(offset) > settings.zt_max_clock_drift_ms:
        msg = f"clock drift {offset} ms exceeds {settings.zt_max_clock_drift_ms} ms; sync NTP"
        if strict:
            raise SystemExit(msg)
        log.warning(msg)
    return lambda: _wall_ms() + offset


async def run_service(mode: str, resume: bool = False) -> None:
    settings = LiveSettings()
    cfg = load_config(settings.zt_config)
    if mode == "live" and (why := settings.live_allowed()):
        raise SystemExit(why)
    lock = acquire_lock(settings.zt_lock_file)  # noqa: F841 - held for the process lifetime
    repo = _repo(settings, cfg)
    if resume:
        repo.set_state("halted", False)
        guard = repo.get_state("guard") or {}
        repo.set_state("guard", {**guard, "halted": False})
    elif mode == "live" and repo.get_state("halted", False):
        raise SystemExit("halted: review the account and the trades, then start with --resume")
    symbols = _symbols(settings, cfg)
    md = load_providers(MARKET_DATA_GROUP, [PROVIDER])[PROVIDER]
    ex = load_providers(EXECUTION_GROUP, [PROVIDER])[PROVIDER]

    async with httpx.AsyncClient() as http:
        if mode == "shadow":
            trading = ex.trading(settings, rest=md.rest(settings))
            clock = await _clock(trading, settings, strict=False)
            alerts = AlertSink(settings, http, repo, clock)
            venue = PaperVenue(
                trading, settings.zt_capital, cfg.exchange.fee_taker, cfg.exchange.leverage
            )
            runner = Runner(cfg, settings, trading, venue, repo, alerts, symbols, clock)
            await runner.startup()
            await alerts("info", "shadow started", [f"{len(symbols)} symbols: {symbols}"])
            await runner.run()
            return

        probe = ex.trading(settings)
        clock = await _clock(probe, settings, strict=True)
        trading = ex.trading(settings, rest=ex.trading_rest(settings, wall_clock_ms=clock))
        alerts = AlertSink(settings, http, repo, clock)
        oms = Oms(
            trading,
            repo,
            alerts,
            leverage=cfg.exchange.leverage,
            fee_taker=cfg.exchange.fee_taker,
            clock_ms=clock,
        )
        venue = LiveVenue(oms, repo, trading, settings.zt_capital, cfg.exchange.leverage)
        runner = Runner(cfg, settings, trading, venue, repo, alerts, symbols, clock)
        await runner.startup()
        recon = Reconciler(trading, repo, oms, alerts, set(symbols), clock)
        repairs = await recon.run_once()
        stream = ex.user_stream(settings, trading.rest, oms.on_event, on_connected=recon.run_once)
        stream_task = asyncio.create_task(stream.run())
        await alerts(
            "warning",
            f"LIVE started ({settings.zt_stage})",
            [f"{len(symbols)} symbols", f"startup repairs: {repairs or 'none'}"],
        )
        try:
            await runner.run(recon)
        finally:
            stream_task.cancel()


async def run_kill() -> list[str]:
    settings = LiveSettings()
    cfg = load_config(settings.zt_config)
    repo = _repo(settings, cfg)
    ex = load_providers(EXECUTION_GROUP, [PROVIDER])[PROVIDER]
    async with httpx.AsyncClient() as http:
        probe = ex.trading(settings)
        clock = await _clock(probe, settings, strict=False)
        trading = ex.trading(settings, rest=ex.trading_rest(settings, wall_clock_ms=clock))
        alerts = AlertSink(settings, http, repo, clock)
        oms = Oms(
            trading,
            repo,
            alerts,
            leverage=cfg.exchange.leverage,
            fee_taker=cfg.exchange.fee_taker,
            clock_ms=clock,
        )
        return await kill(oms, repo, trading, alerts)


def parity(db_url: str, data_dir: Path | None, start: str, end: str, config: Path) -> dict:
    """Replay the recorded period through the backtest core; compare signal ids.

    Needs the period's klines synced from Binance Vision (``zt data sync``).
    """
    cfg = load_config(config)
    repo = Repo(db_url)
    lo = int(pd.Timestamp(start, tz="UTC").value // 1_000_000)
    hi = int(pd.Timestamp(end, tz="UTC").value // 1_000_000)
    rows = [r for r in repo.signals_between(lo, hi) if r["stage"] != "core"]
    live = {r["id"] for r in rows}
    symbols = sorted({r["symbol"] for r in rows})
    paths = DataPaths.from_env(data_dir)
    replayed: set[str] = set()
    for sym in symbols:
        frames = [load(paths, "klines", sym, tf, end_ms=hi) for tf in ("1h", "4h", "1d")]
        for _, step in replay(sym, *frames, cfg):
            if lo <= step.bar.close_time < hi:
                replayed |= {s.id for s in step.result.signals}
    both = live & replayed
    union = live | replayed
    return {
        "live": len(live),
        "backtest": len(replayed),
        "matched": len(both),
        "match_rate": len(both) / len(union) if union else 1.0,
        "only_live": sorted(live - replayed),
        "only_backtest": sorted(replayed - live),
    }
