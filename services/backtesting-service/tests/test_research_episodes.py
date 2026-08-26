from __future__ import annotations

from pathlib import Path

import pytest

from backtesting_service.config import load_settings
from backtesting_service.engine import ClosedBarEngine
from backtesting_service.models import Candle, EngineParams
from backtesting_service.research.episodes import (
    atr_pips_series,
    build_episodes,
    tercile_edges,
    tercile_label,
)
from backtesting_service.sessions import build_windows

FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"
SESSIONS = ["tokyo", "london", "new_york"]


def _candles() -> list[Candle]:
    return [
        Candle.model_validate_json(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _params(**overrides: object) -> EngineParams:
    return EngineParams.model_validate({"orb_minutes": 60, "entry_delay_minutes": 15} | overrides)


def _anchors():
    from backtesting_service.anchors import build_anchors

    return build_anchors(SESSIONS)


def test_episodes_match_the_engine_signal_for_signal() -> None:
    candles = _candles()
    params = _params()
    engine = ClosedBarEngine(build_windows(SESSIONS, {}), params, _anchors())
    engine.run(candles)

    episodes = build_episodes(candles, _anchors(), params)
    engine_signals = {
        (event.session, event.detail["anchor_ts"])
        for event in engine.events
        if event.kind == "signal"
    }

    assert engine_signals == {
        (episode.session, episode.anchor_ts.isoformat()) for episode in episodes
    }


@pytest.mark.parametrize("orb", [15, 30, 60, 120])
def test_episode_agreement_holds_across_opening_range_widths(orb: int) -> None:
    candles = _candles()
    params = _params(orb_minutes=orb)
    engine = ClosedBarEngine(build_windows(SESSIONS, {}), params, _anchors())
    engine.run(candles)

    episodes = build_episodes(candles, _anchors(), params)

    assert len([event for event in engine.events if event.kind == "signal"]) == len(episodes)


def test_episode_fields_are_internally_consistent() -> None:
    params = _params()
    for episode in build_episodes(_candles(), _anchors(), params):
        assert episode.orb_high > episode.orb_low
        assert episode.orb_range_price == pytest.approx(episode.orb_high - episode.orb_low)
        assert episode.orb_range_pips == pytest.approx(episode.orb_range_price / params.pip_size)
        assert episode.orb_bar_count >= 1
        assert episode.anchor_drift_minutes <= params.anchor_tolerance_minutes
        assert episode.entry_ts >= episode.anchor_ts
        assert episode.signal_ts <= episode.entry_ts
        if episode.prior_range_price:
            assert episode.range_expansion == pytest.approx(
                episode.orb_range_price / episode.prior_range_price
            )


def test_atr_series_warms_up_then_tracks_true_range() -> None:
    candles = _candles()
    params = _params()

    series = atr_pips_series(candles, params, period=14)

    assert len(series) == len(candles)
    assert series[:13] == [None] * 13
    assert all(value is not None and value > 0 for value in series[13:])


def test_terciles_split_evenly_and_label_boundaries_low() -> None:
    edges = tercile_edges([float(value) for value in range(1, 10)])

    assert edges is not None
    assert tercile_label(1.0, edges) == "low"
    assert tercile_label(edges[0], edges) == "low"
    assert tercile_label(5.0, edges) == "mid"
    assert tercile_label(9.0, edges) == "high"
    assert tercile_label(None, edges) == "unclassified"
    assert tercile_edges([1.0, 2.0]) is None


def test_local_settings_anchor_set_is_usable_when_present() -> None:
    settings = load_settings(None)

    anchors = settings.session_anchors()

    assert {anchor.name for anchor in anchors} >= {"tokyo", "london", "new_york"}
