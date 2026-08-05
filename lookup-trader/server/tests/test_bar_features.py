"""The bar feature store must agree with the live path and never see the future.

Two invariants carry the whole design. The context half has to reproduce exactly
what `/context` returns for the same bar, or a precomputed base rate describes a
different bar than the one the operator is looking at. And the forward half has
to stay out of the context half, or every probability the store produces is
circular.
"""

from __future__ import annotations

import inspect
import json

import numpy as np
import pandas as pd
import pytest

from app.config import settings
from app.services import bar_features as bf
from app.services.base_rate import outcome_expr
from app.services.context import compute_context
from app.services.labeler import label_triple_barrier
from app.taggers import TagResult, tag_bar

CONTEXT_KEYS = (
    "trend_state",
    "atr_bucket",
    "session",
    "rsi_band",
    "day_of_week",
    "atr_at_signal",
    "ema_slope_bucket",
    "atr_change_bucket",
    "rsi_value",
    "ema_value",
    "dist_ema_atr",
    "context_reliable",
)


def _synthetic(n: int, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.4, n))
    spread = np.abs(rng.normal(0, 0.3, n)) + 0.1
    return pd.DataFrame(
        {
            "ts": pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"),
            "open": close,
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": rng.uniform(50, 150, n),
        }
    )


def _split(candles: pd.DataFrame, idx: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    window = candles.iloc[max(0, idx - settings.warmup_bars + 1) : idx + 1]
    forward = candles.iloc[idx + 1 : idx + 1 + bf.max_horizon()]
    return window, forward


# --------------------------------------------------------------------------
# Parity with the live path
# --------------------------------------------------------------------------


@pytest.mark.parametrize("idx", [300, 450, 699])
def test_context_half_matches_compute_context(idx):
    """A precomputed row and a live /context call must describe the same bar."""
    candles = _synthetic(700)
    window, forward = _split(candles, idx)

    row = bf.compute_bar_row(window, forward, "EURUSD", "H1", pip_size=0.0001)
    expected = compute_context(window.reset_index(drop=True), len(window) - 1)

    for key in CONTEXT_KEYS:
        assert row[key] == expected[key], key


# --------------------------------------------------------------------------
# Leakage
# --------------------------------------------------------------------------


def test_forward_bars_cannot_reach_the_context_half():
    """Truncating the series at the anchor must not move a context value.

    `context_half` is only ever handed the window, so this is structural — the
    test exists to keep it that way if the signature ever changes.
    """
    candles = _synthetic(700)
    idx = 600
    window, forward = _split(candles, idx)

    with_future = bf.compute_bar_row(window, forward, "EURUSD", "H1", 0.0001)
    truncated = bf.compute_bar_row(
        window, candles.iloc[idx + 1 : idx + 1], "EURUSD", "H1", 0.0001
    )

    for key in CONTEXT_KEYS + (
        "context_fingerprint",
        "efficiency_ratio",
        "close_range_pct",
        "bar_tags",
        "tag_setup_ids",
        "tag_primary_setup_id",
        "tag_count",
    ):
        assert with_future[key] == truncated[key], key
    assert with_future["shape_480"] == truncated["shape_480"]


def test_tag_bar_takes_no_forward_data():
    """`tag_bar(window, atr)` — no forward frame, no (frame, index) pair.

    The test above catches leakage after it happens; this one catches the
    opportunity for it. Adding a `forward` parameter would let a tagger read past
    the anchor, so it should be a deliberate act that breaks a test rather than a
    refactor that looks harmless.
    """
    assert list(inspect.signature(tag_bar).parameters) == ["window", "atr_at_bar"]


def test_incomplete_forward_window_is_flagged():
    """The tail of every build has no outcome yet and must say so."""
    candles = _synthetic(300)
    idx = len(candles) - 5
    window, forward = _split(candles, idx)
    row = bf.compute_bar_row(window, forward, "EURUSD", "H1", 0.0001)

    assert row["fwd48_complete"] is False
    assert row["fwd48_bars_available"] == 4
    assert row["fwd6_complete"] is False

    full = bf.compute_bar_row(*_split(candles, 200), "EURUSD", "H1", 0.0001)
    assert full["fwd48_complete"] is True
    assert full["fwd24_complete"] is True


# --------------------------------------------------------------------------
# Tags half
# --------------------------------------------------------------------------


def test_every_row_carries_the_tag_columns():
    """Present on every bar, tagged or not — a NULL here becomes a null-typed
    Parquet column in any month where nothing qualified."""
    candles = _synthetic(700)
    for idx in (300, 450, 699):
        row = bf.compute_bar_row(*_split(candles, idx), "EURUSD", "H1", 0.0001)

        assert row["bar_tags"]["version"] == settings.bar_feature_version
        assert row["tag_count"] == len(row["bar_tags"]["tags"])
        assert isinstance(row["tag_setup_ids"], str)
        assert isinstance(row["tag_primary_setup_id"], str)


def test_tag_columns_agree_with_the_json():
    candles = _synthetic(700)
    row = bf.compute_bar_row(*_split(candles, 600), "EURUSD", "H1", 0.0001)

    result = TagResult.from_json(row["bar_tags"])
    assert row["tag_setup_ids"] == ",".join(result.setup_ids())
    assert row["tag_primary_setup_id"] == result.primary_setup_id()


def test_bar_tags_survive_the_parquet_serialisation():
    """The builder stores this as a JSON string; nothing may be lost in it."""
    candles = _synthetic(700)
    row = bf.compute_bar_row(*_split(candles, 500), "EURUSD", "H1", 0.0001)

    stored = json.dumps(row["bar_tags"], separators=(",", ":"))
    assert TagResult.from_json(stored).to_json() == row["bar_tags"]


# --------------------------------------------------------------------------
# Forward labels
# --------------------------------------------------------------------------


def test_excursions_are_signed_against_the_anchor_close():
    candles = _synthetic(400)
    idx = 300
    window, forward = _split(candles, idx)
    row = bf.compute_bar_row(window, forward, "EURUSD", "H1", 0.0001)

    close0 = float(window["close"].iloc[-1])
    seg = forward.iloc[:24]
    assert row["fwd24_max_pips"] == pytest.approx((float(seg["high"].max()) - close0) / 0.0001)
    assert row["fwd24_min_pips"] == pytest.approx((float(seg["low"].min()) - close0) / 0.0001)
    # Ordering is the whole point: the same two extremes describe a winner and a
    # loser depending on which arrived first.
    assert row["fwd24_max_first"] == (row["fwd24_bars_to_max"] < row["fwd24_bars_to_min"])


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("target,stop", [(1.0, 1.0), (2.0, 1.0), (1.5, 0.5)])
def test_touch_levels_reproduce_the_triple_barrier(side, target, stop):
    """Outcomes derived from stored first-touch bars must match a real labelling.

    This is what lets one precomputed row price any target/stop pair: if the
    derivation drifts from `label_triple_barrier`, the store silently stops
    describing the trades it claims to.
    """
    horizon = 24
    for idx in (250, 300, 350, 400):
        candles = _synthetic(600)
        window, forward = _split(candles, idx)
        row = bf.compute_bar_row(window, forward, "EURUSD", "H1", 0.0001)

        atr = float(row["atr_at_bar"])
        entry = float(row["close"])
        tp = entry + side * target * atr
        sl = entry - side * stop * atr

        labelled = label_triple_barrier(
            candles.reset_index(drop=True),
            idx,
            side,
            entry,
            sl,
            tp,
            max_bars=horizon,
            ambiguous=settings.ambiguous_policy,
        )

        touch = row["level_touch"]
        tp_bar = touch[bf.level_key(target)]["up" if side == 1 else "down"]
        sl_bar = touch[bf.level_key(stop)]["down" if side == 1 else "up"]
        assert _derive(tp_bar, sl_bar, horizon) == labelled["result"]


def _derive(tp_bar, sl_bar, horizon: int) -> str:
    """Python twin of `outcome_expr`, kept beside it so both stay honest."""
    from app.services.labeler import AMBIGUOUS_RESULTS

    tp_hit = tp_bar is not None and tp_bar <= horizon
    sl_hit = sl_bar is not None and sl_bar <= horizon
    if tp_hit and sl_hit and tp_bar == sl_bar:
        return AMBIGUOUS_RESULTS[settings.ambiguous_policy]
    if tp_hit and (not sl_hit or tp_bar < sl_bar):
        return "win"
    if sl_hit and (not tp_hit or sl_bar < tp_bar):
        return "loss"
    return "timeout"


def test_outcome_expr_matches_the_python_derivation():
    """The SQL in base_rate and the reference derivation must not diverge."""
    import duckdb

    con = duckdb.connect(":memory:")
    cases = [(3, None), (None, 3), (3, 3), (None, None), (30, 5), (5, 30), (25, 25)]
    rows = ", ".join(
        "('" + f'{{"1.0":{{"up":{_j(tp)},"down":{_j(sl)}}}}}' + "')" for tp, sl in cases
    )
    con.execute(f"CREATE TABLE t(level_touch VARCHAR); INSERT INTO t VALUES {rows}")

    expr = outcome_expr(horizon=24, target_atr=1.0, stop_atr=1.0, side=1)
    got = [r[0] for r in con.execute(f"SELECT {expr} FROM t").fetchall()]
    assert got == [_derive(tp, sl, 24) for tp, sl in cases]


def _j(value) -> str:
    return "null" if value is None else str(value)


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_shape_is_atr_normalised_and_anchored():
    candles = _synthetic(400)
    idx = 300
    window, forward = _split(candles, idx)
    row = bf.compute_bar_row(window, forward, "EURUSD", "H1", 0.0001)

    close0 = float(window["close"].iloc[-1])
    atr = float(row["atr_at_bar"])
    shape = row["shape_480"]

    assert len(shape) == settings.shape_back_bars * 4
    # The anchor bar is last, and its close is the origin by construction.
    assert shape[-1] == pytest.approx(0.0, abs=1e-12)
    assert shape[-4] == pytest.approx((float(window["open"].iloc[-1]) - close0) / atr)
    assert len(row["shape_48"]) == settings.shape_downsample_groups * 4


def test_shape_pads_the_front_when_history_is_short():
    """Early bars have no 120-bar history; padding keeps the anchor at the end."""
    candles = _synthetic(260)
    idx = 210  # 211 bars of history, enough for the EMA but not for the window
    short = _synthetic(60)

    row = bf.compute_bar_row(*_split(candles, idx), "EURUSD", "H1", 0.0001)
    assert not any(np.isnan(row["shape_480"]))

    thin = bf.compute_bar_row(*_split(short, 40), "EURUSD", "H1", 0.0001)
    padded = np.array(thin["shape_480"])
    assert np.isnan(padded[:4]).all()
    assert not np.isnan(padded[-4:]).any()
    assert thin["back_bars_available"] == 41


def test_shape_is_price_level_invariant():
    """The same shape at a different price must produce the same vector.

    Raw OHLC would make gold at 2,000 and gold at 3,300 incomparable, which is
    the entire reason for normalising.
    """
    candles = _synthetic(400)
    shifted = candles.copy()
    for col in ("open", "high", "low", "close"):
        shifted[col] = shifted[col] + 500.0

    a = bf.compute_bar_row(*_split(candles, 300), "EURUSD", "H1", 0.0001)
    b = bf.compute_bar_row(*_split(shifted, 300), "EURUSD", "H1", 0.0001)
    assert a["shape_480"] == pytest.approx(b["shape_480"])


# --------------------------------------------------------------------------
# New context features
# --------------------------------------------------------------------------


def test_efficiency_ratio_separates_a_trend_from_chop():
    n = 300
    ts = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    trend = np.arange(n, dtype=float) * 0.5 + 100
    chop = 100 + np.tile([0.0, 1.0], n // 2)

    def frame(close):
        return pd.DataFrame(
            {
                "ts": ts,
                "open": close,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": np.ones(n),
            }
        )

    trending = bf.compute_bar_row(*_split(frame(trend), 250), "EURUSD", "H1", 0.0001)
    choppy = bf.compute_bar_row(*_split(frame(chop), 250), "EURUSD", "H1", 0.0001)

    assert trending["efficiency_ratio"] == pytest.approx(1.0)
    assert choppy["efficiency_ratio"] < 0.1


def test_session_bands_are_utc_not_machine_local():
    """Candles arrive tz-aware and pandas renders them locally; a bar at 22:00
    UTC is off_hours regardless of where the builder runs."""
    ts = pd.date_range("2024-01-01", periods=300, freq="h", tz="UTC")
    close = np.linspace(100, 110, 300)
    candles = pd.DataFrame(
        {"ts": ts, "open": close, "high": close + 0.2, "low": close - 0.2,
         "close": close, "volume": np.ones(300)}
    )
    local = candles.copy()
    local["ts"] = local["ts"].dt.tz_convert("America/Chicago")

    idx = 250
    utc_row = bf.compute_bar_row(*_split(candles, idx), "EURUSD", "H1", 0.0001)
    local_row = bf.compute_bar_row(*_split(local, idx), "EURUSD", "H1", 0.0001)

    assert utc_row["session"] == local_row["session"]
    assert utc_row["day_of_week"] == local_row["day_of_week"]
    # 2024-01-01T00:00Z + 250h is 10:00 UTC, inside the London band.
    assert utc_row["session"] == "london"
