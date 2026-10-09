"""Markouts from signal time: moves, gaps, episodes, sources and the summary."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

pd = pytest.importorskip("pandas")

from research.markouts import (  # noqa: E402
    Costs,
    candidate_signals,
    episodes,
    forward_moves,
    load_signal_log,
    markouts,
    summarise_moves,
)

G = 100_000_000  # one grid step
S = 1_000_000_000


def test_forward_moves_are_signed_and_start_at_the_next_grid_sample() -> None:
    t = np.arange(0, 20) * G
    mid = 100.0 * (1 + np.arange(20) * 1e-4)  # +1 bp per step
    moves = forward_moves(
        t, mid, np.array([2 * G, 2 * G + 1]), np.array([1.0, -1.0]), (0.1, 1), grid_ns=G
    )
    assert moves["0.1s"][0] == pytest.approx(1e4 * (mid[3] / mid[2] - 1))
    # The second signal starts at the next sample (step 3) and is a sell.
    assert moves["1s"][1] == pytest.approx(-1e4 * (mid[13] / mid[3] - 1))


def test_forward_moves_are_nan_across_a_gap_past_the_end_or_far_from_a_sample() -> None:
    t = np.concatenate([np.arange(0, 10), np.arange(15, 30)]) * G  # gap after step 9
    mid = np.full(len(t), 100.0)
    at = np.array([5 * G, 16 * G, 28 * G, 12 * G])
    moves = forward_moves(t, mid, at, np.ones(4), (0.1, 1), grid_ns=G)
    assert math.isnan(moves["1s"][0])  # crosses the gap
    assert moves["1s"][1] == 0.0
    assert math.isnan(moves["1s"][2])  # past the end
    assert math.isnan(moves["0.1s"][3])  # no sample within two steps of the signal


def test_episodes_keep_the_first_signal_of_each_run() -> None:
    signals = pd.DataFrame(
        {
            "symbol": ["A", "A", "A", "A", "B"],
            "t_ns": [0, 1 * S, 7 * S, 8 * S, 1 * S],
            "side": ["buy", "buy", "buy", "sell", "buy"],
            "action": ["enter"] * 5,
        }
    )
    kept = episodes(signals, episode_s=5)
    assert list(zip(kept["symbol"], kept["t_ns"], kept["side"], strict=True)) == [
        ("A", 0, "buy"),
        ("B", 1 * S, "buy"),
        ("A", 7 * S, "buy"),
        ("A", 8 * S, "sell"),
    ]


def test_signal_log_and_candidate_sources(tmp_path) -> None:
    records = [
        {"symbol": "A", "t_ns": 5, "side": "buy", "action": "skip:halted"},
        {"symbol": "A", "t_ns": 6, "side": "sell", "action": "enter"},
        {"symbol": "A", "t_ns": 7, "action": "enter"},  # no side: ignored
    ]
    (tmp_path / "2026-10-08.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\nx")
    log = load_signal_log(tmp_path, ["2026-10-08", "2026-10-09"])
    assert list(log["action"]) == ["skip", "enter"] and list(log["side"]) == ["buy", "sell"]

    oof = pd.DataFrame(
        {"symbol": ["A"] * 3, "t_ns": [1, 2, 3], "up": [0.6, 0.1, 0.3], "down": [0.1, 0.7, 0.3]}
    )
    picked = candidate_signals(oof, 0.55)
    assert list(picked["side"]) == ["buy", "sell"] and list(picked["t_ns"]) == [1, 2]


def test_markouts_and_summary() -> None:
    t = np.arange(0, 100) * G
    rows = pd.DataFrame(
        {
            "symbol": ["A"] * 100 + ["B"] * 100,
            "t_ns": np.concatenate([t, t]),
            "mid": np.concatenate([100 * (1 + np.arange(100) * 1e-4), np.full(100, 50.0)]),
        }
    )
    signals = pd.DataFrame(
        {"symbol": ["A", "B"], "t_ns": [0, 0], "side": ["buy", "sell"], "action": ["enter"] * 2}
    )
    frame = markouts(rows, signals, (1,))
    summary = summarise_moves(frame, (1,), Costs(maker_bp=2, taker_bp=5))
    assert summary["by_symbol"]["A"]["1s"]["mean_bp"] == pytest.approx(10.0, abs=0.01)
    assert summary["by_symbol"]["B"]["1s"]["mean_bp"] == 0.0
    assert summary["by_symbol"]["A"]["1s"]["above_market_exit_cost"] == 1.0
    assert summary["overall"]["1s"]["n"] == 2
    assert summary["costs_bp"] == {"market_exit": 7.0, "maker_exit": 4.0}
