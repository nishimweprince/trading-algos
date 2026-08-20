from __future__ import annotations

import hashlib
import json
from pathlib import Path

from engine import ClosedBarEngine
from models import Candle, EngineParams, Timeframe
from sessions import build_windows

FIXTURES = Path(__file__).parent / "fixtures"
PHASE1_BARS = FIXTURES / "xauusd_m15.jsonl"
HEDGE_PAIR_GOLDEN = FIXTURES / "phase1_hedge_pair_golden.json"


def _phase1_bars() -> list[Candle]:
    return [
        Candle.model_validate_json(line)
        for line in PHASE1_BARS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _parity_engine() -> ClosedBarEngine:
    params = EngineParams(
        entry_mode="hedge_pair",
        stop_mode="bar_range",
        cost_model="none",
        risk_mode="fixed_qty",
        intrabar_mode="optimistic",
        time_exit_mode="none",
        qty=1,
        qty_ref=1,
        timeframe_minutes=15,
        orb_minutes=60,
        entry_delay_minutes=15,
        anchor_tolerance_minutes=15,
        one_open_per_session=False,
        max_concurrent_structures=0,
        max_open_risk_pct=0,
    )
    return ClosedBarEngine(build_windows(["tokyo", "london", "new_york"], {}), params)


def _phase1_payload(engine: ClosedBarEngine) -> dict[str, object]:
    report = engine.report("XAUUSD", Timeframe.M15, "local").model_dump(mode="json")
    # ENTRY_MODE did not exist in the captured Phase 1 report. Everything else below is the
    # complete pre-refactor payload, including ordered trades/events and grouped pair ordering.
    report.pop("entry_mode")
    trades = report.pop("trades")
    events = report.pop("events")
    pairs = report.pop("trade_pairs")
    return {
        "stats": engine.stats.model_dump(mode="json"),
        "trades": trades,
        "events": events,
        "trade_pairs": pairs,
        "report": report,
    }


def test_hedge_pair_matches_phase1_golden_bit_for_bit() -> None:
    golden = json.loads(HEDGE_PAIR_GOLDEN.read_text(encoding="utf-8"))
    engine = _parity_engine()
    engine.run(_phase1_bars())
    payload = _phase1_payload(engine)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    assert hashlib.sha256(canonical).hexdigest() == golden["canonical_sha256"]
    assert engine.stats.model_dump(mode="json") == golden["stats"]
    assert [pair["id"] for pair in payload["trade_pairs"]] == golden["pair_ids"]  # type: ignore[index]
    assert [
        [trade["pair_id"], trade["side"], trade["ts"]]
        for trade in payload["trades"]  # type: ignore[union-attr]
    ] == golden["trade_order"]
    assert [event["kind"] for event in payload["events"]] == golden["event_kinds"]  # type: ignore[union-attr]
