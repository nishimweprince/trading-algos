"""Bootstrap DuckDB schema, candles view, and seed setups."""

from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.db.duck import get_connection, register_candles_view

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

SEED_SETUPS = """
INSERT INTO setups (setup_id, name, default_side)
SELECT * FROM (VALUES
  ('bull_engulfing', 'Bullish Engulfing', 1),
  ('bear_engulfing', 'Bearish Engulfing', -1),
  ('pin_bar_long',   'Bullish Pin Bar',   1),
  ('inside_break',   'Inside Bar Break',  NULL)
) AS v(setup_id, name, default_side)
WHERE NOT EXISTS (SELECT 1 FROM setups LIMIT 1);
"""


def bootstrap() -> None:
    con = get_connection()
    schema_sql = SCHEMA_PATH.read_text()
    con.execute(schema_sql)
    register_candles_view(con)
    con.execute(SEED_SETUPS)
    con.close()
    print(f"Bootstrapped {settings.duckdb_path}")


if __name__ == "__main__":
    bootstrap()
