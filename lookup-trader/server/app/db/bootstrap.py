"""Bootstrap DuckDB schema, candles view, and seed setups."""

from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.db.duck import get_connection, register_candles_view
from app.db.setups_seed import SEED_SETUPS

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# CREATE TABLE IF NOT EXISTS is a no-op on a database that predates the column.
ADD_CATEGORY = "ALTER TABLE setups ADD COLUMN IF NOT EXISTS category VARCHAR;"

OCCURRENCE_MIGRATIONS = [
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS pips_captured DOUBLE;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS observed_trend VARCHAR;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS confluence_tags VARCHAR;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS screenshot_entry VARCHAR;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS screenshot_exit VARCHAR;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS metadata JSON;",
]


def seed_setups(con) -> None:
    """Insert any missing setup, and backfill the category of ones already there.

    Per-row rather than all-or-nothing: the catalog grows over time and new
    patterns have to reach databases that were seeded before they existed.
    """
    for setup_id, name, default_side, category in SEED_SETUPS:
        con.execute(
            "INSERT INTO setups (setup_id, name, default_side, category) "
            "SELECT ?, ?, ?, ? "
            "WHERE NOT EXISTS (SELECT 1 FROM setups WHERE setup_id = ?)",
            [setup_id, name, default_side, category, setup_id],
        )
        con.execute(
            "UPDATE setups SET category = ? WHERE setup_id = ? AND category IS NULL",
            [category, setup_id],
        )


def bootstrap() -> None:
    con = get_connection()
    schema_sql = SCHEMA_PATH.read_text()
    con.execute(schema_sql)
    con.execute(ADD_CATEGORY)
    for migration in OCCURRENCE_MIGRATIONS:
        con.execute(migration)
    register_candles_view(con)
    seed_setups(con)
    con.close()
    print(f"Bootstrapped {settings.duckdb_path}")


if __name__ == "__main__":
    bootstrap()
