#!/usr/bin/env python3
"""Reset every derived artefact for one pair and rebuild it from the candle store.

The candle store is the input, not an output: this script never fetches, ingests
or modifies H1 candles. Drop them into `data/candles/symbol=<SYM>/timeframe=H1/`
by hand (or via `ingest_histdata.py` / `sync_capital.py`) and everything
downstream is regenerated from them.

    candles/H1  →  quality audit  →  candles/H4  →  features  →  meta-events
      (input)       derived          derived       derived       derived

Hand-labelled trades are *not* derived and are preserved by default. `occurrences`,
`signals` and `labeling_sessions` are the one thing here no rebuild can
reconstruct, so wiping them takes an explicit `--wipe-labels` and still writes a
parquet backup first.

Dry run by default, matching `reset_database.py`: it prints what it would delete
and rebuild, and changes nothing until you pass `--yes`.

    ./scripts/rebuild_pipeline.py                    # show the plan
    ./scripts/rebuild_pipeline.py --yes --skip-train # reset and rebuild/export
    ./scripts/rebuild_pipeline.py --yes --wipe-labels

Stop the dev server first if you pass `--wipe-labels` — DuckDB holds an
exclusive lock on `engine.duckdb`. Every other stage reads parquet through an
in-memory DuckDB and runs happily while the API is up.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import duckdb

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from app.config import settings
from app.db.bootstrap import bootstrap
from app.db.duck import close_all, get_connection
from app.services.candle_audit import write_audit
from app.services.h4_resample import rebuild_h4
from app.services.meta_events_v2 import (
    event_manifest_path_v2,
    event_path_v2,
    event_report_path_v2,
    export_meta_events_v2,
)
from build_bar_features import build as build_features
from reset_database import _backup_occurrences

# The outcome-v1 contract is enforced inside `export_bar_features._validate_contract`,
# not merely in argparse: the dataset, the feature list and the trained artifact
# are all specified for this pair and timeframe. Widening it is a modelling
# decision, so this script refuses rather than producing something mislabelled.
SUPPORTED = {("XAUUSD", "H1")}

# Derived candles. H1 is the input; H4 is resampled from it and rebuilt here so a
# manually-dropped H1 file cannot leave a stale higher timeframe beside it.
DERIVED_TIMEFRAMES = ("H4",)


def _supported_str() -> str:
    return " / ".join(f"{sym} {tf}" for sym, tf in sorted(SUPPORTED))


class Stage:
    """A named step, so the plan and the run print the same list in the same order."""

    def __init__(self, name: str, detail: str) -> None:
        self.name, self.detail = name, detail


def _fmt_size(path: Path) -> str:
    if not path.exists():
        return "absent"
    if path.is_file():
        return f"{path.stat().st_size / 1e6:.1f} MB"
    files = list(path.rglob("*"))
    size = sum(f.stat().st_size for f in files if f.is_file())
    return f"{sum(1 for f in files if f.is_file())} files, {size / 1e6:.1f} MB"


def _artifact_dirs(symbol: str) -> list[Path]:
    """Model artifacts belonging to this pair, by naming convention."""
    root = settings.outcome_artifact_root
    if not root.is_dir():
        return []
    prefix = f"{symbol.lower()}-"
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith(prefix))


def _stamp_version(symbol: str, timeframe: str) -> str:
    """A fresh immutable artifact version. Artifacts are never overwritten."""
    day = datetime.now(UTC).strftime("%Y%m%d")
    root = settings.outcome_artifact_root
    base = f"{symbol.lower()}-{timeframe.lower()}-outcome-v1-rebuild-{day}"
    for revision in range(1, 100):
        candidate = f"{base}-r{revision}"
        if not (root / candidate).exists():
            return candidate
    raise RuntimeError(f"Ran out of revisions for {base}")


# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------


def _targets(symbol: str, timeframe: str, wipe_labels: bool) -> list[tuple[str, Path]]:
    """Everything the reset removes, in the order it removes it."""
    candle_root = settings.data_dir / "candles" / f"symbol={symbol}"
    exports = settings.data_dir / "exports"
    out: list[tuple[str, Path]] = []
    for derived in DERIVED_TIMEFRAMES:
        out.append((f"derived {derived} candles", candle_root / f"timeframe={derived}"))
    # Scoped to the timeframe being rebuilt. Removing the whole `symbol=` tree
    # would delete feature sets this run does not regenerate.
    out.append(
        (
            "bar features",
            settings.features_dir / f"symbol={symbol}" / f"timeframe={timeframe}",
        )
    )
    out.append(("legacy training matrix", exports / "bar_features_training.parquet"))
    out.append(
        ("legacy training manifest", exports / "bar_features_training.manifest.json")
    )
    out.append(
        (
            "candle exclusions",
            exports / f"candle-exclusions-{symbol}-{timeframe}-v1.json",
        )
    )
    out.append(
        (
            "candle audit",
            settings.data_dir
            / "reports"
            / f"candle-audit-{symbol}-{timeframe}-v1.json",
        )
    )
    out.append(("meta events v2", event_path_v2()))
    out.append(("meta event manifest v2", event_manifest_path_v2()))
    out.append(("meta event report v2", event_report_path_v2(symbol, timeframe)))
    if wipe_labels:
        out.append(("labelling database", settings.duckdb_path))
    return out


def _candle_summary(symbol: str, timeframe: str) -> tuple[int, str, str]:
    """Rows and date range of the input candles, read straight from parquet."""
    base = settings.data_dir / "candles" / f"symbol={symbol}" / f"timeframe={timeframe}"
    files = sorted(base.glob("year=*/month=*/part-*.parquet"))
    if not files:
        raise SystemExit(
            f"No {timeframe} candles at {base}\n"
            f"Add them first — this script rebuilds from the candle store, it does not fetch."
        )
    con = duckdb.connect(":memory:")
    try:
        rows, first, last = con.execute(
            f"SELECT count(*), min(ts), max(ts) FROM read_parquet('{base}/**/*.parquet')"
        ).fetchone()
    finally:
        con.close()
    return rows, str(first), str(last)


def _other_feature_timeframes(symbol: str, timeframe: str) -> list[str]:
    """Feature timeframes for this pair that the run will leave untouched."""
    root = settings.features_dir / f"symbol={symbol}"
    if not root.is_dir():
        return []
    return sorted(
        p.name.removeprefix("timeframe=")
        for p in root.iterdir()
        if p.is_dir() and p.name != f"timeframe={timeframe}"
    )


def _label_counts() -> dict[str, int] | str | None:
    """Row counts of the operator tables.

    None if there is no database, the string "locked" if the API holds it. The
    counts are informational, so a running dev server must not stop the plan
    from printing — only `--wipe-labels` genuinely needs that lock.
    """
    if not settings.duckdb_path.exists():
        return None
    try:
        con = get_connection()
    except (OSError, duckdb.IOException):
        return "locked"
    try:
        return {
            table: con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("occurrences", "signals", "labeling_sessions")
        }
    finally:
        con.close()


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------


def plan(symbol: str, timeframe: str, *, wipe_labels: bool, skip_train: bool) -> None:
    rows, first, last = _candle_summary(symbol, timeframe)
    print(f"Input   {symbol} {timeframe}: {rows} candles, {first} → {last}\n")

    print("Remove")
    for label, path in _targets(symbol, timeframe, wipe_labels):
        rel = path.relative_to(_REPO_ROOT) if path.is_relative_to(_REPO_ROOT) else path
        print(f"  {label:22} {rel!s:58} {_fmt_size(path)}")

    others = _other_feature_timeframes(symbol, timeframe)
    if others:
        print(
            f"\n  Note: {symbol} also has features for {', '.join(others)}. This run neither"
            f"\n  removes nor rebuilds them, so they will be stale against changed candles."
            f"\n  Re-run with --timeframe for each."
        )

    labels = _label_counts()
    print("\nLabelled data (never derived from candles)")
    if labels is None:
        print("  no database yet")
    elif labels == "locked":
        print("  counts unavailable — the dev server holds the database lock")
        if wipe_labels:
            print("  --wipe-labels needs that lock. Stop ./scripts/dev.sh first.")
    else:
        counts = ", ".join(f"{k}={v}" for k, v in labels.items())
        if wipe_labels:
            print(f"  WIPING {counts}")
            print(
                "  A parquet backup of occurrences is written to data/exports/ first."
            )
        else:
            print(f"  preserved: {counts}")

    print("\nRebuild")
    for stage in _stages(
        symbol, timeframe, skip_train=skip_train, wipe_labels=wipe_labels
    ):
        print(f"  {stage.name:22} {stage.detail}")


def _stages(
    symbol: str, timeframe: str, *, skip_train: bool, wipe_labels: bool
) -> list[Stage]:
    out = [
        Stage("candle audit", "accept source candles and write exclusion intervals"),
        Stage("derive H4", f"resample {symbol} H1 → H4 candles"),
        Stage(
            "bar features",
            f"{symbol} {timeframe} at bar_feature_version {settings.bar_feature_version}",
        ),
        Stage("bootstrap db", "schema, seeded setups, candle and feature views"),
    ]
    if wipe_labels:
        out.insert(0, Stage("reset db", "back up occurrences, delete, re-bootstrap"))
    if (symbol, timeframe) in SUPPORTED:
        out.append(
            Stage("meta events", "export calendar-enhanced meta-events v2; preserve v1")
        )
        out.append(
            Stage(
                "model training",
                "separate immutable shadow command (--skip-train required here)",
            )
        )
    else:
        out.append(
            Stage("training matrix", f"skipped — outcome-v1 is {_supported_str()} only")
        )
    return out


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def reset(symbol: str, timeframe: str, *, wipe_labels: bool) -> None:
    if wipe_labels and settings.duckdb_path.exists():
        con = get_connection()
        try:
            _backup_occurrences(con)
        finally:
            con.close()
        close_all()
        Path(f"{settings.duckdb_path}.wal").unlink(missing_ok=True)

    for label, path in _targets(symbol, timeframe, wipe_labels):
        if path.exists():
            _remove(path)
            print(f"  removed {label}: {path}")


def rebuild(
    symbol: str, timeframe: str, *, skip_train: bool, model_version: str | None
) -> str | None:
    candle_root = settings.data_dir / "candles"

    print("\n[1/5] Auditing source candles…")
    audit_report, exclusions, report = write_audit(symbol, timeframe)
    print(
        f"  {report['status']}: {', '.join(report['excluded_months']) or 'no exclusions'}"
    )
    print(f"  wrote {audit_report}")
    print(f"  wrote {exclusions}")

    print("\n[2/5] Deriving H4 candles from H1…")
    print(f"  wrote {rebuild_h4(candle_root, symbol)} H4 candles")

    print(f"\n[3/5] Building {symbol} {timeframe} bar features…")
    built = build_features(
        symbol=symbol,
        timeframe=timeframe,
        output_dir=settings.features_dir,
        min_warmup=settings.ema_period,
        since=None,
        rebuild=True,
        dry_run=False,
    )
    print(f"  wrote {built} feature rows")

    print("\n[4/5] Bootstrapping the database…")
    try:
        bootstrap()
        print("  schema, setups and views are current")
    except (OSError, duckdb.IOException):
        # Only reachable while preserving labels — the wipe path already took
        # the lock to back occurrences up, so it would have failed earlier.
        # The running API bootstrapped on startup and the views glob the parquet
        # at query time, so it will see the rebuilt features without help.
        print("  skipped — the dev server holds the lock and has already bootstrapped")

    if (symbol, timeframe) not in SUPPORTED:
        print(
            f"\n[5/5] Skipping meta-event export — v2 covers {_supported_str()} only."
        )
        return None

    print("\n[5/5] Exporting automated meta-events…")
    count = export_meta_events_v2(symbol, timeframe)
    print(f"  wrote {count} rows to {event_path_v2()}")
    print("  model training remains a separate immutable-artifact step")
    return None


def _report(version: str | None) -> None:
    """Tell the operator how to point inference at the artifact just trained.

    Artifacts are immutable and the version is resolved at import time, so
    nothing here can switch it over for a process that is already running.
    """
    if version is None:
        return
    print("\n" + "─" * 78)
    if version == settings.outcome_artifact_version:
        print("Inference already points at this artifact — nothing further to do.")
        return

    print("The new artifact is immutable and inference still points at the old one:")
    print(f"  {settings.outcome_artifact_version}\n")
    # Whichever layer is currently winning is the one worth editing — telling
    # someone to change a config default that their .env is overriding sends
    # them to a line that will have no effect.
    default = type(settings).model_fields["outcome_artifact_version"].default
    if settings.outcome_artifact_version != default:
        print("That value comes from the environment. Update server/.env:\n")
        print(f"    LOOKUP_OUTCOME_ARTIFACT_VERSION={version}")
    else:
        print("Update the default in server/app/config.py:\n")
        print(f'    outcome_artifact_version: str = "{version}"')
    print("\nThen restart the API.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument(
        "--yes", action="store_true", help="apply changes (default is a dry run)"
    )
    parser.add_argument(
        "--wipe-labels",
        action="store_true",
        help="also delete hand-labelled trades, after backing occurrences up to parquet",
    )
    parser.add_argument(
        "--skip-train", action="store_true", help="stop after the training export"
    )
    parser.add_argument(
        "--model-version",
        default=None,
        help="name the artifact instead of auto-stamping it; must not already exist",
    )
    args = parser.parse_args()
    symbol, timeframe = args.symbol.upper(), args.timeframe.upper()

    try:
        plan(
            symbol, timeframe, wipe_labels=args.wipe_labels, skip_train=args.skip_train
        )
        if not args.yes:
            print("\nDry run. Re-run with --yes to apply.")
            return 0

        if not args.skip_train:
            print(
                "Pipeline rebuilds never train or rotate meta-model artifacts. "
                "Re-run with --yes --skip-train, then use "
                "scripts/build_meta_shadow_artifacts.py explicitly.",
                file=sys.stderr,
            )
            return 2

        print("\nResetting…")
        reset(symbol, timeframe, wipe_labels=args.wipe_labels)
        version = rebuild(
            symbol,
            timeframe,
            skip_train=args.skip_train,
            model_version=args.model_version,
        )
        _report(version)
        print("\nDone.")
        return 0
    except (OSError, duckdb.IOException) as exc:
        print(f"Database is locked or unavailable: {exc}", file=sys.stderr)
        print("Stop the dev server (./scripts/dev.sh) and try again.", file=sys.stderr)
        return 1
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
