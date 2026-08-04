"""CLI entry point to promote pending occurrences after max_bars."""

from app.db.bootstrap import bootstrap
from app.db.duck import get_connection, register_candles_view
from app.services.pending import resolve_pending_occurrences


def main() -> None:
    bootstrap()
    con = get_connection()
    register_candles_view(con)
    resolved = resolve_pending_occurrences(con)
    con.close()
    print(f"Resolved {len(resolved)} pending occurrence(s)")


if __name__ == "__main__":
    main()
