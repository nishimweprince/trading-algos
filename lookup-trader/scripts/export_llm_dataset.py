#!/usr/bin/env python3
"""Export meta-events, their economic-news window, and outcomes as LLM training data.

    ./scripts/export_llm_dataset.py --dry-run    # counts, splits, token estimate
    ./scripts/export_llm_dataset.py --yes        # writes the JSONL splits

One JSONL record per meta-event, in the chat format OpenAI, Azure AI Foundry and
the open-weights trainers (TRL, Axolotl, Unsloth) all read. The prompt carries
the causal market context plus the news the operator could have seen; the
completion carries the take/skip label and the realised outcome.

Why this export exists rather than reusing the tabular columns directly: the
tabular pipeline compresses the calendar to six numbers — a count, two gaps and
three flags. Those say *how much* news is nearby, never *which*. "FOMC Statement
in 90 minutes" and "German Buba Speech in 90 minutes" are identical to CatBoost
and obviously different to a language model. That semantic content is the one
thing an LLM can use here that gradient boosting provably cannot, so the news
titles are the point of the whole file.

Causality is enforced, not assumed:

  * Scheduled fields (time, currency, impact, title) are published days ahead
    and are safe at any bar before the release.
  * `actual` / `forecast` / `previous` are only known from
    `release_values_available_at_utc`, so they appear only for releases that had
    already happened at `signal_ts`.
  * No outcome column reaches the prompt. `_assert_causal` re-reads every
    rendered prompt and fails the export if an outcome token appears.

Splits are chronological. A random split would put 2015 in test and 2019 in
train, which for a time series is simply leakage.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings  # noqa: E402
from app.services.calendar.store import events_parquet_path  # noqa: E402
from app.services.meta_events_v2 import event_path_v2  # noqa: E402

# Chronological boundaries. Test is 2025 onward to match the rest of the
# project, but see the warning `main` prints: taking every event earns +0.0335R
# there against -0.0515R before it, so absolute test performance means nothing.
# Only lift over take-all on the same block does.
TRAIN_UNTIL_YEAR = 2022
VALIDATION_YEARS = (2023, 2024)

# Hours of calendar either side of the signal. Backwards is what just moved the
# market; forwards is the trade's own 24-bar lifetime, and scheduled events in
# that window are known at signal time.
NEWS_LOOKBACK = timedelta(hours=24)
NEWS_LOOKAHEAD = timedelta(hours=24)

# Anything below this is noise at gold's scale, and including all 81k events
# would swamp the prompt.
NEWS_IMPACTS = ("high", "medium")
NEWS_CURRENCIES = ("USD", "EUR", "CNY", "GBP", "JPY")
MAX_NEWS_ROWS = 12

SYSTEM_PROMPT = (
    "You are evaluating a single XAUUSD H1 trade candidate produced by a "
    "deterministic pattern detector. The trade enters at the next H1 open with a "
    "2 ATR stop and a 3 ATR target (1.5 R:R), and is marked to market after 24 "
    "bars. Costs are 3 pips round trip. Decide whether taking it is expected to "
    "be profitable after costs. Answer with JSON only."
)

# Any of these appearing in a prompt means the future leaked in.
_OUTCOME_TOKENS = (
    "net_r", "gross_r", "y_meta", "exit_price", "exit_ts", "outcome",
    "bars_to_resolution", "target_price", "stop_price",
)

_BUCKET_FEATURES = (
    "trend_state", "htf_trend_state", "session", "rsi_band", "atr_bucket",
    "htf_atr_bucket", "ema_slope_bucket", "atr_change_bucket", "day_of_week",
)
_NUMERIC_FEATURES = (
    "rsi_value", "atr_pct", "dist_ema_atr", "ema_slope_atr", "atr_change_ratio",
    "efficiency_ratio", "close_range_pct", "realized_vol_atr",
    "round_number_dist_atr", "dist_day_high_atr", "dist_day_low_atr",
    "dist_swing_high_atr", "dist_swing_low_atr", "prior_day_high_dist_atr",
    "prior_day_low_dist_atr", "prior_week_high_dist_atr",
    "prior_week_low_dist_atr", "gap_atr", "signal_body_pct",
    "signal_upper_wick_pct", "signal_lower_wick_pct", "signal_range_atr",
    "bars_since_swing_high", "bars_since_swing_low", "htf_range_pct",
)


def output_dir() -> Path:
    return settings.data_dir / "exports" / "llm"


def metadata_path(split: str) -> Path:
    """Sidecar with outcome truth. Evaluation reads this; nothing uploads it."""
    return output_dir() / f"meta-events-{split}.metadata.jsonl"


# The upload format is narrow and the platform validates it only after the file
# has been accepted and queued, so a structural mistake costs a round trip and a
# confusing "contains invalid schema" against every line. Checking locally turns
# that into a one-second failure.
_ALLOWED_ROLES = {"system", "user", "assistant"}


def validate_upload(path: Path, limit: int = 20) -> list[str]:
    """Structural problems that would make a fine-tuning platform reject the file."""
    problems: list[str] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if len(problems) >= limit:
                break
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                problems.append(f"line {number}: not valid JSON ({exc.msg})")
                continue
            extra = sorted(set(row) - {"messages"})
            if extra:
                problems.append(f"line {number}: unexpected top-level key(s) {extra}")
                continue
            messages = row.get("messages")
            if not isinstance(messages, list) or not messages:
                problems.append(f"line {number}: `messages` must be a non-empty list")
                continue
            roles = [message.get("role") for message in messages]
            if set(roles) - _ALLOWED_ROLES:
                problems.append(f"line {number}: unsupported role(s) {sorted(set(roles))}")
            elif roles[-1] != "assistant":
                problems.append(f"line {number}: last message must be the assistant completion")
            elif any(not str(message.get("content", "")).strip() for message in messages):
                problems.append(f"line {number}: a message has empty content")
    return problems


def _clean(value: Any) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if hasattr(value, "item"):
        value = value.item()
    return value


def _news_window(calendar: pd.DataFrame, signal_ts: pd.Timestamp) -> list[dict[str, Any]]:
    """Scheduled releases around the signal, with values only where already public."""
    lower = signal_ts - NEWS_LOOKBACK
    upper = signal_ts + NEWS_LOOKAHEAD
    window = calendar.loc[
        (calendar["time_utc"] >= lower)
        & (calendar["time_utc"] <= upper)
        & calendar["impact"].isin(NEWS_IMPACTS)
        & calendar["currency"].isin(NEWS_CURRENCIES)
    ]
    if window.empty:
        return []
    # Nearest to the signal first, then truncate: a prompt listing forty low-value
    # releases buries the two that matter.
    window = window.assign(
        _distance=(window["time_utc"] - signal_ts).abs()
    ).nsmallest(MAX_NEWS_ROWS, "_distance")

    rows: list[dict[str, Any]] = []
    for row in window.sort_values("time_utc").itertuples(index=False):
        minutes = int(round((row.time_utc - signal_ts).total_seconds() / 60))
        entry: dict[str, Any] = {
            "title": row.title,
            "currency": row.currency,
            "impact": row.impact,
            "minutes_from_signal": minutes,
        }
        # The causality line. Schedule is public in advance; the numbers are not
        # until the release lands, and `release_values_available_at_utc` is when
        # that was. Reading them earlier would hand the model the future.
        available_at = row.release_values_available_at_utc
        if pd.notna(available_at) and available_at <= signal_ts:
            for name in ("actual", "forecast", "previous"):
                value = _clean(getattr(row, name))
                if value is not None:
                    entry[name] = value
        rows.append(entry)
    return rows


def _prompt(event: pd.Series, news: list[dict[str, Any]]) -> str:
    context = {name: _clean(event.get(name)) for name in _BUCKET_FEATURES}
    numeric = {
        name: (round(float(v), 4) if (v := _clean(event.get(name))) is not None else None)
        for name in _NUMERIC_FEATURES
    }
    payload = {
        "signal_utc": pd.Timestamp(event["signal_ts"]).isoformat(),
        "side": "long" if int(event["side"]) == 1 else "short",
        "primary_setup": event["primary_setup_id"],
        "confluence_setups": json.loads(event["setup_ids"])
        if isinstance(event["setup_ids"], str)
        else list(event["setup_ids"]),
        "detector_match_quality": round(float(event["confidence"]), 4),
        # Every regime and distance field is already expressed relative to the
        # proposed side, so "aligned" means favourable for this trade rather
        # than "price is going up".
        "market_context": context,
        "measurements_atr_normalised": numeric,
        "economic_calendar": news,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)


def _completion(event: pd.Series) -> str:
    """Just the decision.

    An earlier version also emitted `outcome` and `net_r_3`. Both are wrong to
    train on: the realised R is a continuous value the model cannot know at
    signal time, so reproducing it burns capacity on an unpredictable regression
    and destabilises the classification this is actually for. They live in
    `metadata` instead, where the evaluator can reach them.

    Keeping the boolean as the final token also means `logprobs` yields a
    calibrated probability rather than a bare decision, which is what the
    threshold sweep and log loss need.
    """
    return json.dumps({"take": bool(int(event["y_meta"]))}, separators=(",", ":"))


def _assert_causal(prompt: str, event_id: str) -> None:
    lowered = prompt.lower()
    leaked = sorted(token for token in _OUTCOME_TOKENS if token in lowered)
    if leaked:
        raise AssertionError(f"Outcome leaked into the prompt for {event_id}: {leaked}")


def _split_of(year: int) -> str:
    if year <= TRAIN_UNTIL_YEAR:
        return "train"
    if year in VALIDATION_YEARS:
        return "validation"
    return "test"


def build(events: pd.DataFrame, calendar: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    splits: dict[str, list[dict[str, Any]]] = {"train": [], "validation": [], "test": []}
    for event in events.itertuples(index=False):
        row = pd.Series(event._asdict())
        signal_ts = pd.Timestamp(row["signal_ts"])
        prompt = _prompt(row, _news_window(calendar, signal_ts))
        _assert_causal(prompt, str(row["event_id"]))
        splits[_split_of(signal_ts.year)].append(
            {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": _completion(row)},
                ],
                # Written to a SIDECAR file, never into the upload. An earlier
                # version put this alongside `messages` on the same line, on the
                # assumption trainers would ignore an unknown key. Azure's
                # validator rejects the whole file instead — "contains invalid
                # schema" against every one of the 4,000 lines. The training
                # JSONL must contain `messages` and nothing else.
                "metadata": {
                    "event_id": row["event_id"],
                    "signal_ts": signal_ts.isoformat(),
                    "primary_setup_id": row["primary_setup_id"],
                    "side": int(row["side"]),
                    # Outcome truth for the evaluator, deliberately outside
                    # `messages` so no trainer learns to reproduce it.
                    "outcome": row["outcome"],
                    "y_meta": int(row["y_meta"]),
                    "net_r_3": round(float(row["net_r_3"]), 4),
                    "net_r_5": round(float(row["net_r_5"]), 4),
                    "net_r_8": round(float(row["net_r_8"]), 4),
                },
            }
        )
    return splits


def _estimate_tokens(records: list[dict[str, Any]]) -> int:
    """Rough count at ~4 characters per token; enough to size a training run."""
    characters = sum(
        len(message["content"]) for record in records for message in record["messages"]
    )
    return characters // 4


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="write the files")
    parser.add_argument("--dry-run", action="store_true", help="counts only (default)")
    parser.add_argument("--limit", type=int, default=None, help="cap events, for a smoke test")
    parser.add_argument(
        "--max-train",
        type=int,
        default=None,
        help=(
            "Keep only the most recent N training records. A first experiment "
            "does not need 13M tokens to show whether there is signal, and the "
            "recent years are the ones whose regime resembles the test block. "
            "Check the printed date range: older regimes are dropped, not thinned."
        ),
    )
    args = parser.parse_args()

    events = pd.read_parquet(event_path_v2()).sort_values("signal_ts").reset_index(drop=True)
    if args.limit:
        events = events.head(args.limit)
    calendar = pd.read_parquet(events_parquet_path())
    calendar["time_utc"] = pd.to_datetime(calendar["time_utc"], utc=True)
    calendar["release_values_available_at_utc"] = pd.to_datetime(
        calendar["release_values_available_at_utc"], utc=True
    )
    events["signal_ts"] = pd.to_datetime(events["signal_ts"], utc=True)

    print(f"events {len(events):,}   calendar rows {len(calendar):,}")
    splits = build(events, calendar)

    if args.max_train and len(splits["train"]) > args.max_train:
        # The most recent records, not a stride across the whole period. The
        # training split is already chronological, so this is the tail.
        #
        # The trade is deliberate: recent years match the regime the model will
        # be judged on, at the cost of never showing it the 2013 crash or the
        # 2015 bear. The printed date range below says which years survived, and
        # it is worth reading — if the kept window is all one direction, the
        # model learns that direction rather than the setup.
        dropped = len(splits["train"]) - args.max_train
        splits["train"] = splits["train"][-args.max_train :]
        kept_from = splits["train"][0]["metadata"]["signal_ts"][:7]
        print(
            f"thinned train to the latest {args.max_train:,} "
            f"(dropped {dropped:,} older records; kept from {kept_from})"
        )

    print(f"\n{'split':12} {'records':>9} {'~tokens':>12} {'positive':>9}  range")
    for name in ("train", "validation", "test"):
        records = splits[name]
        if not records:
            continue
        stamps = [record["metadata"]["signal_ts"][:7] for record in records]
        positive = sum(record["metadata"]["y_meta"] for record in records)
        print(
            f"{name:12} {len(records):>9,} {_estimate_tokens(records):>12,} "
            f"{positive / len(records):>8.1%}  {min(stamps)} → {max(stamps)}"
        )

    news_counts = [
        len(json.loads(record["messages"][1]["content"])["economic_calendar"])
        for records in splits.values()
        for record in records
    ]
    print(
        f"\ncalendar rows per prompt: mean {sum(news_counts) / len(news_counts):.1f}, "
        f"max {max(news_counts)}, empty for {news_counts.count(0)} events"
    )

    if not args.yes:
        print("\nDry run. Re-run with --yes to write.")
        return 0

    root = output_dir()
    root.mkdir(parents=True, exist_ok=True)
    for name, records in splits.items():
        path = root / f"meta-events-{name}.jsonl"
        sidecar = metadata_path(name)
        with path.open("w", encoding="utf-8") as upload, sidecar.open(
            "w", encoding="utf-8"
        ) as meta:
            for record in records:
                # Upload gets `messages` and nothing else.
                upload.write(
                    json.dumps({"messages": record["messages"]}, separators=(",", ":")) + "\n"
                )
                meta.write(json.dumps(record["metadata"], separators=(",", ":")) + "\n")
        problems = validate_upload(path)
        status = "OK" if not problems else f"{len(problems)} INVALID LINES"
        print(f"Wrote {path} ({len(records):,} records) — schema {status}")
        for problem in problems[:3]:
            print(f"    {problem}")
        print(f"Wrote {sidecar} ({len(records):,} rows, evaluation only — never upload)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
