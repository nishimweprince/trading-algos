#!/usr/bin/env python3
"""Reset DuckDB labelling data from the repo root.

By default this backs up and clears the occurrences table only (setups and
labeling_sessions are left alone). Use --full to delete engine.duckdb and
re-bootstrap a fresh database. Parquet candles on disk are never touched.

Stop the dev server before running — DuckDB holds an exclusive file lock.

    python3 scripts/reset_database.py              # dry run, reports counts
    python3 scripts/reset_database.py --yes        # back up, then clear occurrences
    python3 scripts/reset_database.py --full --yes   # back up, delete DB, re-bootstrap
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings  # noqa: E402
from app.db.bootstrap import bootstrap  # noqa: E402
from app.db.duck import close_all, get_connection  # noqa: E402


def _backup_occurrences(con) -> Path | None:
    total = con.execute("SELECT count(*) FROM occurrences").fetchone()[0]
    if total == 0:
        return None

    out_dir = settings.data_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = out_dir / f"occurrences_backup_{stamp}.parquet"

    con.execute(f"COPY occurrences TO '{backup}' (FORMAT PARQUET)")
    written = con.execute(f"SELECT count(*) FROM read_parquet('{backup}')").fetchone()[0]
    if written != total:
        raise RuntimeError(f"Backup wrote {written} of {total} rows — refusing to continue")
    print(f"Backed up {written} occurrences to {backup}")
    return backup


def _report_occurrences(con) -> int:
    total = con.execute("SELECT count(*) FROM occurrences").fetchone()[0]
    if total == 0:
        print("occurrences: 0 rows")
        return 0

    by_source = con.execute(
        "SELECT source, count(*) FROM occurrences GROUP BY source ORDER BY 1"
    ).fetchall()
    print(f"occurrences: {total} rows (" + ", ".join(f"{s}={n}" for s, n in by_source) + ")")
    return total


def reset_occurrences(*, apply: bool) -> int:
    con = get_connection()
    try:
        total = _report_occurrences(con)
        sessions = con.execute("SELECT count(*) FROM labeling_sessions").fetchone()[0]
        print(f"labeling_sessions: {sessions} rows (left unchanged)")

        if total == 0:
            print("Nothing to clear.")
            return 0

        if not apply:
            print("\nDry run. Re-run with --yes to back up and clear occurrences.")
            return 0

        _backup_occurrences(con)
        con.execute("DELETE FROM occurrences")
        print(f"Cleared occurrences ({total} rows removed)")
        return 0
    finally:
        con.close()


def reset_full(*, apply: bool) -> int:
    db_path = settings.duckdb_path
    wal_path = Path(f"{db_path}.wal")

    if db_path.exists():
        con = get_connection()
        try:
            _report_occurrences(con)
            sessions = con.execute("SELECT count(*) FROM labeling_sessions").fetchone()[0]
            print(f"labeling_sessions: {sessions} rows")
        finally:
            con.close()
    else:
        print(f"No database at {db_path}")

    if not apply:
        print("\nDry run. Re-run with --full --yes to back up, delete, and re-bootstrap.")
        return 0

    if db_path.exists():
        con = get_connection()
        try:
            _backup_occurrences(con)
        finally:
            con.close()

    close_all()
    db_path.unlink(missing_ok=True)
    wal_path.unlink(missing_ok=True)
    print(f"Deleted {db_path}")

    bootstrap()
    print("Database reset complete.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="apply changes (default is dry run)")
    parser.add_argument(
        "--full",
        action="store_true",
        help="delete engine.duckdb and re-bootstrap instead of clearing occurrences only",
    )
    args = parser.parse_args()

    try:
        if args.full:
            return reset_full(apply=args.yes)
        return reset_occurrences(apply=args.yes)
    except (OSError, duckdb.IOException) as exc:
        print(f"Database is locked or unavailable: {exc}", file=sys.stderr)
        print("Stop the dev server (./scripts/dev.sh) and try again.", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
