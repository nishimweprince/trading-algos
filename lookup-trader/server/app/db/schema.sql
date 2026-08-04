CREATE TABLE IF NOT EXISTS setups (
  setup_id      VARCHAR PRIMARY KEY,
  name          VARCHAR NOT NULL,
  description   VARCHAR,
  default_side  INTEGER,
  category      VARCHAR,
  active        BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS labeling_sessions (
  session_id  UUID DEFAULT uuid() PRIMARY KEY,
  started_at  TIMESTAMP DEFAULT now(),
  ended_at    TIMESTAMP,
  symbol      VARCHAR,
  timeframe   VARCHAR,
  date_from   TIMESTAMP,
  date_to     TIMESTAMP,
  blinded     BOOLEAN DEFAULT FALSE,
  notes       VARCHAR
);

CREATE TABLE IF NOT EXISTS occurrences (
  id                  UUID DEFAULT uuid() PRIMARY KEY,
  source              VARCHAR NOT NULL,
  session_id          UUID,
  symbol              VARCHAR NOT NULL,
  timeframe           VARCHAR NOT NULL,
  ts                  TIMESTAMP NOT NULL,
  setup_id            VARCHAR NOT NULL,
  side                INTEGER NOT NULL,
  entry               DOUBLE,
  sl                  DOUBLE,
  tp                  DOUBLE,
  max_bars            INTEGER,
  atr_period          INTEGER,
  atr_at_signal       DOUBLE,
  result              VARCHAR,
  realized_r          DOUBLE,
  bars_to_resolution  INTEGER,
  observed_result     VARCHAR,
  trend_state         VARCHAR,
  atr_bucket          VARCHAR,
  session             VARCHAR,
  rsi_band            VARCHAR,
  calendar_flag       BOOLEAN,
  calendar_tags         VARCHAR,
  notes               VARCHAR,
  labeler_version     VARCHAR,
  pips_captured       DOUBLE,
  observed_trend      VARCHAR,
  confluence_tags     VARCHAR,
  screenshot_entry    VARCHAR,
  screenshot_exit     VARCHAR,
  metadata            JSON,
  created_at          TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_occ_lookup
  ON occurrences (setup_id, symbol, timeframe, trend_state, session);
