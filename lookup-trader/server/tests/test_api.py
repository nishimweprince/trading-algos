from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_symbols_and_setups():
    r = client.get("/symbols")
    assert r.status_code == 200
    assert "EURUSD" in r.json()

    r = client.get("/setups")
    assert r.status_code == 200
    setups = r.json()
    assert len(setups) >= 4


def test_candlestick_setups_cover_both_directions():
    """Every rule tagger needs a seeded id, or its tags name a setup /setups
    cannot resolve to a display name."""
    setups = {s["setup_id"]: s for s in client.get("/setups").json()}

    for setup_id in ("bull_engulfing", "bear_engulfing", "pin_bar_long", "pin_bar_short"):
        assert setup_id in setups, setup_id
        assert setups[setup_id]["category"] == "candlestick"

    assert setups["pin_bar_short"]["default_side"] == -1
    assert setups["pin_bar_long"]["default_side"] == 1
    # The break itself decides direction, so the setup carries none.
    assert setups["inside_break"]["default_side"] is None


def test_context_returns_bar_tags():
    symbol, timeframe = "XAUUSD", "H1"
    bounds = client.get("/candles/bounds", params={"symbol": symbol, "timeframe": timeframe}).json()
    if not bounds["bar_count"]:
        pytest.skip(f"no {symbol} {timeframe} candles ingested")

    r = client.get(
        "/context",
        params={"symbol": symbol, "timeframe": timeframe, "signal_ts": bounds["max_ts"]},
    )
    assert r.status_code == 200
    body = r.json()

    assert isinstance(body["bar_tags"], list)
    assert body["tag_source"] in {"store", "live"}
    for tag in body["bar_tags"]:
        assert tag["state"] in {"complete", "forming", "invalidated"}
        assert 0.0 <= tag["confidence"] <= 1.0
        assert tag["source"] in {"rule", "algorithm"}

    primary = body["tag_primary_setup_id"]
    if primary is None:
        assert not [t for t in body["bar_tags"] if t["state"] == "complete"]
    else:
        # Never the store's empty-string sentinel — that is a None up here.
        complete_ids = [t["setup_id"] for t in body["bar_tags"] if t["state"] == "complete"]
        assert primary and primary in complete_ids


def test_base_rate_rejects_unknown_tag_state():
    r = client.get(
        "/base-rate",
        params={
            "symbol": "XAUUSD",
            "timeframe": "H1",
            "signal_ts": "2026-01-01T00:00:00Z",
            "tag_setup_id": "double_bottom",
            "tag_state": "predicted",
        },
    )
    assert r.status_code == 422


def test_candle_bounds():
    r = client.get("/candles/bounds", params={"symbol": "EURUSD", "timeframe": "H1"})
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {
        "min_ts", "max_ts", "bar_count", "htf_available", "htf_timeframe"
    }
    if body["bar_count"] > 0:
        assert body["min_ts"] is not None
        assert body["max_ts"] is not None
        assert body["min_ts"] <= body["max_ts"]
    else:
        assert body["min_ts"] is None
        assert body["max_ts"] is None


def test_candles_and_session_flow():
    r = client.get(
        "/candles",
        params={
            "symbol": "EURUSD",
            "timeframe": "H1",
            "date_from": "2024-01-01T00:00:00Z",
            "date_to": "2024-12-31T23:59:59Z",
        },
    )
    assert r.status_code == 200
    candles = r.json()
    assert len(candles) >= 1

    r = client.post(
        "/sessions",
        json={
            "symbol": "EURUSD",
            "timeframe": "H1",
            "date_from": "2024-01-01T00:00:00Z",
            "date_to": "2024-12-31T23:59:59Z",
            "blinded": False,
        },
    )
    assert r.status_code == 200
    session_id = r.json()["session_id"]

    signal_ts = candles[0]["ts"]
    r = client.post(
        "/trades",
        json={
            "session_id": session_id,
            "symbol": "EURUSD",
            "timeframe": "H1",
            "signal_ts": signal_ts,
            "setup_id": "bull_engulfing",
            "side": 1,
            "entry": 1.1002,
            "sl": 1.0990,
            "tp": 1.1020,
            "date_from": "2024-01-01T00:00:00Z",
            "date_to": "2024-12-31T23:59:59Z",
        },
    )
    assert r.status_code == 200
    trade = r.json()
    assert trade["source"] == "manual"
    assert trade["result"] in ("win", "loss", "timeout", "ambiguous")

    r = client.post(
        "/compare",
        json={
            "setup_id": "bull_engulfing",
            "symbol": "EURUSD",
            "timeframe": "H1",
            "context": {"trend_state": "up", "session": "asian"},
            "min_samples": 1,
        },
    )
    assert r.status_code == 200
    assert "level_used" in r.json()


# EURUSD is a two-bar smoke fixture; XAUUSD has enough history to exercise
# indicator warmup and the forward window.
SYMBOL = "XAUUSD"
DATE_FROM = "2026-01-01T00:00:00Z"
DATE_TO = "2026-12-31T23:59:59Z"


def _new_session() -> str:
    r = client.post(
        "/sessions",
        json={
            "symbol": SYMBOL,
            "timeframe": "H1",
            "date_from": DATE_FROM,
            "date_to": DATE_TO,
            "blinded": False,
        },
    )
    return r.json()["session_id"]


def _signal_ts(offset: int) -> str:
    r = client.get(
        "/candles",
        params={
            "symbol": SYMBOL,
            "timeframe": "H1",
            "date_from": DATE_FROM,
            "date_to": DATE_TO,
        },
    )
    return r.json()[offset]["ts"]


def _levels() -> dict:
    """Long levels around the gold price in the middle of the fixture window."""
    return {"entry": 4313.7, "sl": 4300.0, "tp": 4340.0}


def test_trade_records_path_and_provenance():
    r = client.post(
        "/trades",
        json={
            "session_id": _new_session(),
            "symbol": SYMBOL,
            "timeframe": "H1",
            "signal_ts": _signal_ts(250),
            "setup_id": "bull_engulfing",
            "side": 1,
            **_levels(),
            "blinded": False,
            "provenance": {
                "peeked": True,
                "max_cursor_before_arm": 40,
                "decision_ms": 8200,
                "level_revisions": 3,
                "bars_visible_at_signal": 5,
            },
        },
    )
    assert r.status_code == 200, r.text
    trade = r.json()

    assert trade["outcome_kind"] == "traded"
    assert trade["exit_price"] is not None
    assert trade["r_at_horizon"] is not None
    assert trade["mfe_r"] is not None and trade["mae_r"] is not None
    assert set(trade["r_grid"]) == {"0.5", "1.0", "1.5", "2.0", "3.0", "5.0"}
    assert trade["feature_version"]

    # Provenance: a peeked label has to stay identifiable.
    assert trade["peeked"] is True
    assert trade["features"]["decision_ms"] == 8200
    assert trade["features"]["level_revisions"] == 3
    assert trade["features"]["entry_next_open"] is not None


def test_levels_on_the_wrong_side_are_rejected():
    body = {
        "session_id": _new_session(),
        "symbol": SYMBOL,
        "timeframe": "H1",
        "signal_ts": _signal_ts(251),
        "setup_id": "bull_engulfing",
        "side": 1,
        "entry": 4313.7,
        "sl": 4340.0,  # stop above entry on a long
        "tp": 4300.0,
        "observed_result": "win",
    }
    assert client.post("/trades", json=body).status_code == 422

    body["side"] = 7
    assert client.post("/trades", json=body).status_code == 422


def test_skip_is_recorded_but_not_compared():
    session_id = _new_session()
    before = client.post(
        "/compare",
        json={
            "setup_id": "pin_bar_long",
            "symbol": SYMBOL,
            "timeframe": "H1",
            "context": {},
            "min_samples": 1,
        },
    ).json()

    r = client.post(
        "/trades",
        json={
            "session_id": session_id,
            "symbol": SYMBOL,
            "timeframe": "H1",
            "signal_ts": _signal_ts(252),
            "setup_id": "pin_bar_long",
            "side": 1,
            "outcome_kind": "skipped",
            "skip_reason": "setup_poor_location",
            "notes": "right pattern, wrong place",
        },
    )
    assert r.status_code == 200, r.text
    skip = r.json()
    assert skip["outcome_kind"] == "skipped"
    assert skip["skip_reason"] == "setup_poor_location"
    assert skip["result"] is None
    # Context is still computed — that is the point of recording a negative.
    assert skip["trend_state"] in ("up", "down")

    after = client.post(
        "/compare",
        json={
            "setup_id": "pin_bar_long",
            "symbol": SYMBOL,
            "timeframe": "H1",
            "context": {},
            "min_samples": 1,
        },
    ).json()
    assert after["matched_count"] == before["matched_count"]


def test_skip_requires_a_reason():
    r = client.post(
        "/trades",
        json={
            "session_id": _new_session(),
            "symbol": SYMBOL,
            "timeframe": "H1",
            "signal_ts": _signal_ts(253),
            "setup_id": "pin_bar_long",
            "side": 1,
            "outcome_kind": "skipped",
        },
    )
    assert r.status_code == 422


def test_patch_edits_labels_but_not_the_verdict():
    created = client.post(
        "/trades",
        json={
            "session_id": _new_session(),
            "symbol": SYMBOL,
            "timeframe": "H1",
            "signal_ts": _signal_ts(254),
            "setup_id": "bull_engulfing",
            "side": 1,
            **_levels(),
        },
    ).json()

    r = client.patch(
        f"/trades/{created['id']}",
        json={"notes": "misread the level", "observed_trend": "range", "result": "win"},
    )
    assert r.status_code == 200
    patched = r.json()
    assert patched["notes"] == "misread the level"
    assert patched["observed_trend"] == "range"
    assert patched["result"] == created["result"]  # labeler keeps the last word

    r = client.delete(f"/trades/{created['id']}", params={"reason": "duplicate"})
    assert r.status_code == 200
    assert r.json()["excluded"] is True
    assert r.json()["exclude_reason"] == "duplicate"

    assert client.patch("/trades/00000000-0000-0000-0000-000000000000", json={}).status_code == 404


def test_context_endpoint_agrees_with_the_labeler():
    """The panel pre-fills from this, so it has to return what /trades computes
    for the same bar — otherwise the operator compares against a context the
    stored occurrence never had."""
    signal_ts = _signal_ts(300)
    ctx = client.get(
        "/context",
        params={"symbol": SYMBOL, "timeframe": "H1", "signal_ts": signal_ts},
    )
    assert ctx.status_code == 200, ctx.text
    context = ctx.json()

    trade = client.post(
        "/trades",
        json={
            "session_id": _new_session(),
            "symbol": SYMBOL,
            "timeframe": "H1",
            "signal_ts": signal_ts,
            "setup_id": "bull_engulfing",
            "side": 1,
            **_levels(),
        },
    ).json()

    for field in ("trend_state", "atr_bucket", "session", "rsi_band"):
        assert context[field] == trade[field], field
    assert context["atr_at_signal"] == pytest.approx(trade["atr_at_signal"])
    assert context["context_reliable"] is True


def test_context_works_at_the_last_bar():
    """No forward bars exist there. The endpoint asks for none — fetching them
    would hand the UI the window the chart is deliberately hiding."""
    candles = client.get(
        "/candles",
        params={"symbol": SYMBOL, "timeframe": "H1", "date_from": DATE_FROM, "date_to": DATE_TO},
    ).json()

    r = client.get(
        "/context",
        params={"symbol": SYMBOL, "timeframe": "H1", "signal_ts": candles[-1]["ts"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["trend_state"] in ("up", "down")


def test_context_rejects_a_timestamp_that_is_not_a_bar():
    r = client.get(
        "/context",
        params={"symbol": SYMBOL, "timeframe": "H1", "signal_ts": "2026-06-15T22:17:00Z"},
    )
    assert r.status_code == 404


def test_promoted_labels_become_queryable_columns():
    """metadata stays the submitted record; the columns are what /compare filters."""
    trade = client.post(
        "/trades",
        json={
            "session_id": _new_session(),
            "symbol": SYMBOL,
            "timeframe": "H1",
            "signal_ts": _signal_ts(301),
            "setup_id": "bull_engulfing",
            "side": 1,
            **_levels(),
            "metadata": {
                "market_structure": "continuation",
                "htf_alignment": "aligned",
                "entry_quality": "clean",
                "confidence": 4,
            },
        },
    ).json()

    assert trade["market_structure"] == "continuation"
    assert trade["htf_alignment"] == "aligned"
    assert trade["entry_quality"] == "clean"
    assert trade["confidence"] == 4
    assert trade["metadata"]["confidence"] == 4  # blob untouched
    # entry 4313.7 / sl 4300 / tp 4340 -> planned R:R 1.92
    assert trade["rr_bucket"] == "standard"
    assert trade["sl_atr_bucket"] in ("tight", "normal", "wide")

    patched = client.patch(
        f"/trades/{trade['id']}",
        json={"metadata": {"market_structure": "reversal", "confidence": 2}},
    ).json()
    assert patched["market_structure"] == "reversal"
    assert patched["confidence"] == 2
    # Dropped from the blob, so it must be dropped from the column too.
    assert patched["entry_quality"] is None


def test_signal_snapshot_and_trade_link():
    session_id = _new_session()
    signal_ts = _signal_ts(255)

    r = client.post(
        "/signals",
        json={
            "session_id": session_id,
            "symbol": SYMBOL,
            "timeframe": "H1",
            "signal_ts": signal_ts,
            "setup_id": "bull_engulfing",
            "side": 1,
            "cursor_idx": 255,
            "bars_visible": 256,
            "peeked": False,
            "compare_context": {
                "trend_state": "up",
                "session": "asian",
                "ema_slope_bucket": "up",
                "atr_change_bucket": "contracting",
                "consolidation_before": True,
                "day_of_week": "wed",
            },
            "compare_min_samples": 1,
            "annotations": {
                "confidence": 4,
                "at_key_level": True,
                "level_type": "prior_swing_high",
                "consolidation_before": True,
            },
        },
    )
    assert r.status_code == 200, r.text
    signal = r.json()
    assert signal["context_snapshot"]["day_of_week"] is not None
    assert signal["context_fingerprint"]
    assert signal["compare_at_signal"] is not None
    assert signal["at_key_level"] is True

    trade = client.post(
        "/trades",
        json={
            "session_id": session_id,
            "signal_id": signal["id"],
            "symbol": SYMBOL,
            "timeframe": "H1",
            "signal_ts": signal_ts,
            "setup_id": "bull_engulfing",
            "side": 1,
            **_levels(),
        },
    ).json()

    assert trade["signal_id"] == signal["id"]
    assert trade["compare_at_signal"] is not None
    assert trade["day_of_week"] is not None
    assert trade["lifecycle"] == "resolved"
    assert trade["features"].get("next_open_label") is not None


def test_export_flattens_json_columns():
    r = client.get("/export", params={"format": "csv"})
    assert r.status_code == 200
    header = r.text.splitlines()[0].split(",")
    assert "features_pip_size" in header
    assert "r_grid_2.0" in header
    assert "features" not in header  # raw JSON dropped from the CSV
