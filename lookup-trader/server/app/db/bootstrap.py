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
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS exit_ts TIMESTAMP;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS exit_price DOUBLE;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS r_at_horizon DOUBLE;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS net_r DOUBLE;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS ambiguous_bar BOOLEAN;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS entry_feasible BOOLEAN;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS mfe_r DOUBLE;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS mae_r DOUBLE;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS mfe_pips DOUBLE;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS mae_pips DOUBLE;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS bars_to_mfe INTEGER;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS bars_to_mae INTEGER;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS r_grid JSON;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS outcome_kind VARCHAR DEFAULT 'traded';",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS skip_reason VARCHAR;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS blinded BOOLEAN;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS peeked BOOLEAN;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS context_reliable BOOLEAN;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS excluded BOOLEAN DEFAULT FALSE;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS exclude_reason VARCHAR;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS feature_version VARCHAR;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS features JSON;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS market_structure VARCHAR;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS htf_alignment VARCHAR;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS entry_quality VARCHAR;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS confidence INTEGER;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS rr_bucket VARCHAR;",
    "ALTER TABLE occurrences ADD COLUMN IF NOT EXISTS sl_atr_bucket VARCHAR;",
    # Rows written before outcome_kind existed are all real trades.
    "UPDATE occurrences SET outcome_kind = 'traded' WHERE outcome_kind IS NULL;",
]

# The lookup index has gained columns over time; CREATE INDEX IF NOT EXISTS
# leaves the narrower one in place on a database that already has it.
REINDEX_OCCURRENCES = [
    "DROP INDEX IF EXISTS idx_occ_lookup;",
    "CREATE INDEX IF NOT EXISTS idx_occ_lookup ON occurrences "
    "(setup_id, symbol, timeframe, source, outcome_kind, side, trend_state, session);",
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
    for statement in REINDEX_OCCURRENCES:
        con.execute(statement)
    # Rebuilt on every boot so a changed parquet layout is picked up.
    register_candles_view(con, force=True)
    seed_setups(con)
    con.close()
    print(f"Bootstrapped {settings.duckdb_path}")


if __name__ == "__main__":
    bootstrap()
