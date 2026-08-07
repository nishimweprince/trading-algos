from __future__ import annotations

import pandas as pd

from app.services import candle_audit


def _synthetic_months() -> pd.DataFrame:
    frames = []
    for year in (2022, 2023, 2024):
        for month in range(1, 13):
            count = 70 if year == 2023 and 2 <= month <= 7 else 100
            ts = pd.date_range(f"{year}-{month:02d}-01", periods=count, freq="h", tz="UTC")
            frames.append(
                pd.DataFrame(
                    {
                        "ts": ts,
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.5,
                        "volume": 0.0,
                    }
                )
            )
    return pd.concat(frames, ignore_index=True).sort_values("ts").reset_index(drop=True)


def test_audit_flags_only_low_coverage_months_with_multiple_gaps(monkeypatch):
    frame = _synthetic_months()
    gaps = []
    for month in range(2, 8):
        for day in (2, 3, 4):
            gaps.append(
                {
                    "after": f"2023-{month:02d}-{day:02d}T00:00:00+00:00",
                    "before": f"2023-{month:02d}-{day:02d}T04:00:00+00:00",
                    "hours": 4.0,
                }
            )
    monkeypatch.setattr(candle_audit, "_load", lambda *_: frame)
    monkeypatch.setattr(candle_audit, "_source_files", lambda *_: [])
    monkeypatch.setattr(candle_audit, "unexpected_gaps", lambda *_: gaps)

    report, exclusion = candle_audit.build_audit("XAUUSD", "H1")

    assert report["excluded_months"] == [f"2023-{month:02d}" for month in range(2, 8)]
    assert report["status"] == "accepted_with_exclusions"
    assert len(exclusion["expanded_intervals"]) == 1
    interval = exclusion["expanded_intervals"][0]
    assert interval["pre_padding_bars"] == 48
    assert interval["post_padding_bars"] == 600
    assert all(
        not row["excluded"]
        for row in report["monthly_coverage"]
        if row["year"] == 2023 and row["month"] in {1, 8, 9, 10, 11, 12}
    )
