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

CREATE TABLE IF NOT EXISTS signals (
  id                  UUID DEFAULT uuid() PRIMARY KEY,
  source              VARCHAR NOT NULL DEFAULT 'manual',
  session_id          UUID,
  symbol              VARCHAR NOT NULL,
  timeframe           VARCHAR NOT NULL,
  ts                  TIMESTAMP NOT NULL,
  setup_id            VARCHAR,
  side                INTEGER,
  cursor_idx          INTEGER,
  bars_visible        INTEGER,
  peeked              BOOLEAN,
  blinded             BOOLEAN,
  feature_version     VARCHAR,
  context_snapshot    JSON,
  compare_context     JSON,
  compare_at_signal   JSON,
  -- Base rate over every bar sharing this context, frozen at annotation time so
  -- the prior the decision was made against survives a later rethreshold.
  base_rate_at_signal JSON,
  -- The base rate was visible when the operator decided, so this row is evidence
  -- about the model as much as about the market.
  assisted            BOOLEAN,
  context_fingerprint VARCHAR,
  screenshot_signal   VARCHAR,
  chart_render_spec   JSON,
  -- pre-trade operator annotations
  confluence_tags     VARCHAR,
  calendar_flag       BOOLEAN,
  calendar_tags       VARCHAR,
  confidence          INTEGER,
  at_key_level        BOOLEAN,
  level_type          VARCHAR,
  consolidation_before BOOLEAN,
  annotation_sources  JSON,
  lifecycle           VARCHAR DEFAULT 'open',
  occurrence_id       UUID,
  created_at          TIMESTAMP DEFAULT now()
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
  -- resolved exit
  exit_ts             TIMESTAMP,
  exit_price          DOUBLE,
  r_at_horizon        DOUBLE,             -- mark-to-close at exit; populated for every result
  net_r               DOUBLE,             -- realized_r less the assumed spread
  ambiguous_bar       BOOLEAN,            -- a bar hit both barriers, whatever the policy chose
  entry_feasible      BOOLEAN,            -- marked entry was actually traded through
  -- path
  mfe_r               DOUBLE,
  mae_r               DOUBLE,
  mfe_pips            DOUBLE,
  mae_pips            DOUBLE,
  bars_to_mfe         INTEGER,
  bars_to_mae         INTEGER,
  r_grid              JSON,               -- outcome per target multiple against the same SL
  -- comparison dimensions promoted out of the JSON columns so /compare can
  -- filter and index on them; metadata/features keep the raw submitted values
  market_structure    VARCHAR,
  htf_alignment       VARCHAR,
  entry_quality       VARCHAR,
  confidence          INTEGER,
  rr_bucket           VARCHAR,            -- planned reward/risk
  sl_atr_bucket       VARCHAR,            -- stop width in ATR units
  -- record kind
  outcome_kind        VARCHAR DEFAULT 'traded',   -- traded | skipped
  skip_reason         VARCHAR,
  -- provenance / honesty
  blinded             BOOLEAN,
  peeked              BOOLEAN,            -- operator saw past the signal bar before arming
  context_reliable    BOOLEAN,            -- enough warmup history for the indicators
  excluded            BOOLEAN DEFAULT FALSE,
  exclude_reason      VARCHAR,
  feature_version     VARCHAR,
  features            JSON,               -- machine-computed; metadata is operator-supplied
  signal_id           UUID,
  lifecycle           VARCHAR DEFAULT 'resolved',
  compare_at_signal   JSON,
  context_fingerprint VARCHAR,
  entry_convention    VARCHAR DEFAULT 'marked',
  day_of_week         VARCHAR,
  ema_slope_bucket    VARCHAR,
  atr_change_bucket   VARCHAR,
  htf_trend_state     VARCHAR,
  at_key_level        BOOLEAN,
  level_type          VARCHAR,
  consolidation_before BOOLEAN,
  tagger_confidence   VARCHAR,
  payload_hash        VARCHAR,
  tagger_model_version VARCHAR,
  -- Inherited from the linked signal; see the signals table for both.
  base_rate_at_signal JSON,
  assisted            BOOLEAN,
  created_at          TIMESTAMP DEFAULT now()
);

-- idx_occ_lookup is created in bootstrap.py, after the ALTER migrations: on a
-- database that predates a column the index references, it cannot be built here.
