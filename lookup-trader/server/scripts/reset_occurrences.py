"""Back up and clear the occurrences table.

The context features written before the labeling window became a fixed bar count
are not reproducible — they depended on whichever date range the operator's
session happened to span — so rows from that era cannot be pooled with new ones.
This empties the table so labelling can restart on consistent footing.

Setups and labeling_sessions are left alone. The backup is written first and the
truncate is skipped if it fails.

    python3 -m scripts.reset_occurrences            # dry run, reports counts
    python3 -m scripts.reset_occurrences --yes      # back up, then truncate
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.config import settings
from app.db.duck import get_connection


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="actually delete after backing up")
    args = parser.parse_args()

    con = get_connection()
    try:
        total = con.execute("SELECT count(*) FROM occurrences").fetchone()[0]
        if total == 0:
            print("occurrences is already empty — nothing to do")
            return 0

        by_source = con.execute(
            "SELECT source, count(*) FROM occurrences GROUP BY source ORDER BY 1"
        ).fetchall()
        print(f"{total} occurrences: " + ", ".join(f"{s}={n}" for s, n in by_source))

        if not args.yes:
            print("\nDry run. Re-run with --yes to back up and clear.")
            return 0

        out_dir = settings.data_dir / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = out_dir / f"occurrences_backup_{stamp}.parquet"

        con.execute(f"COPY occurrences TO '{backup}' (FORMAT PARQUET)")
        written = con.execute(f"SELECT count(*) FROM read_parquet('{backup}')").fetchone()[0]
        if written != total:
            print(f"Backup wrote {written} of {total} rows — refusing to delete", file=sys.stderr)
            return 1
        print(f"Backed up {written} rows to {backup}")

        con.execute("DELETE FROM occurrences")
        print(f"Cleared occurrences ({total} rows removed)")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
