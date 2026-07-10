import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

/**
 * SQLite persistence (Section 10). Uses Node's built-in `node:sqlite`
 * (DatabaseSync) — synchronous, zero-ops, and requires no native compilation
 * (better-sqlite3 does not build against current Node). Same ergonomics: sync
 * prepared statements, fine for our low write rate. Schema is created
 * idempotently on open.
 *
 * Secrets (wallet key) are NEVER written here (Section 8).
 */

export type DB = DatabaseSync;

const SCHEMA = `
CREATE TABLE IF NOT EXISTS graduations (
  mint                TEXT NOT NULL,
  slot                INTEGER,
  detected_at_ns      TEXT NOT NULL,             -- process.hrtime.bigint() as string
  feed_source         TEXT NOT NULL,             -- grpc | pumpportal
  venue               TEXT,                       -- pumpswap | raydium
  pool_address        TEXT,
  detection_latency_ms REAL,
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (mint, slot)
);

CREATE TABLE IF NOT EXISTS candidates (
  mint                TEXT NOT NULL,
  enrichment_json     TEXT,                       -- raw enrichment snapshot
  hard_check_results  TEXT,                       -- JSON array of CheckResult
  soft_score          REAL,
  verdict             TEXT,                       -- accept | veto
  veto_reasons        TEXT,                       -- JSON array of strings
  high_volatility     INTEGER NOT NULL DEFAULT 0,
  relaxed_risk        INTEGER NOT NULL DEFAULT 0,
  relaxed_reasons_json TEXT,
  sellability_reason  TEXT,
  created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_candidates_mint ON candidates(mint);

CREATE TABLE IF NOT EXISTS positions (
  mint                        TEXT NOT NULL,
  entry_tx                    TEXT,
  entry_price                 REAL,
  exit_price                  REAL,
  size_sol                    REAL,
  state                       TEXT NOT NULL,      -- PENDING_ENTRY | OPEN | EXITING | CLOSED | FAILED
  exit_reason                 TEXT,               -- ExitTrigger
  exit_tx                     TEXT,
  pnl_sol                     REAL,
  pnl_pct                     REAL,
  opened_at                   TEXT,
  closed_at                   TEXT,
  exit_trigger_to_confirm_ms  REAL,
  execution_json              TEXT,
  exit_intent_json            TEXT,
  relaxed_risk                INTEGER NOT NULL DEFAULT 0,
  relaxed_reasons_json        TEXT,
  created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_positions_mint ON positions(mint);
CREATE INDEX IF NOT EXISTS idx_positions_state ON positions(state);

CREATE TABLE IF NOT EXISTS price_ticks (
  mint         TEXT NOT NULL,
  slot         INTEGER,
  price        REAL,
  sol_reserve  REAL,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_price_ticks_mint ON price_ticks(mint, created_at);

CREATE TABLE IF NOT EXISTS blacklisted_creators (
  address    TEXT PRIMARY KEY,
  reason     TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS blacklisted_mints (
  mint       TEXT PRIMARY KEY,
  reason     TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS blacklisted_funding_sources (
  address    TEXT PRIMARY KEY,
  reason     TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS breaker_events (
  type       TEXT NOT NULL,
  detail     TEXT,
  tripped    INTEGER NOT NULL DEFAULT 1,
  at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS operator_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  category     TEXT NOT NULL,
  level        TEXT NOT NULL,
  message      TEXT NOT NULL,
  entity_mint  TEXT,
  payload_json TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_operator_events_created ON operator_events(created_at);
CREATE INDEX IF NOT EXISTS idx_operator_events_category ON operator_events(category);
CREATE INDEX IF NOT EXISTS idx_operator_events_level ON operator_events(level);

CREATE TABLE IF NOT EXISTS latency_samples (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  kind         TEXT NOT NULL,               -- detection | exit_confirm | entry_confirm
  mint         TEXT,
  feed_source  TEXT,
  latency_ms   REAL NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_latency_kind_time ON latency_samples(kind, created_at);

CREATE TABLE IF NOT EXISTS analytics_snapshots (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  period               TEXT NOT NULL,         -- hour | day
  period_start         TEXT NOT NULL,
  mode                 TEXT NOT NULL,
  realized_pnl_sol     REAL,
  trade_count          INTEGER,
  wins                 INTEGER,
  losses               INTEGER,
  expectancy_sol       REAL,
  profit_factor        REAL,
  max_drawdown_sol     REAL,
  graduations          INTEGER,
  accepted             INTEGER,
  vetoed               INTEGER,
  entered              INTEGER,
  failed               INTEGER,
  emergency_exits      INTEGER,
  detection_p50_ms     REAL,
  detection_p95_ms     REAL,
  exit_confirm_p50_ms  REAL,
  exit_confirm_p95_ms  REAL,
  fees_sol             REAL,
  payload_json         TEXT,
  created_at           TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(period, period_start, mode)
);

CREATE TABLE IF NOT EXISTS candidate_check_results (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  mint         TEXT NOT NULL,
  check_id     TEXT NOT NULL,
  status       TEXT NOT NULL,               -- pass | fail | unknown
  detail       TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_check_results_check ON candidate_check_results(check_id, status);
CREATE INDEX IF NOT EXISTS idx_check_results_mint ON candidate_check_results(mint);

CREATE TABLE IF NOT EXISTS run_sessions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at    TEXT NOT NULL DEFAULT (datetime('now')),
  mode          TEXT NOT NULL,
  config_hash   TEXT NOT NULL,
  config_json   TEXT NOT NULL,
  git_commit    TEXT,
  ended_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_sessions_started ON run_sessions(started_at);

CREATE TABLE IF NOT EXISTS position_fills (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  mint                TEXT NOT NULL,
  session_id          INTEGER,
  trigger             TEXT NOT NULL,
  fraction            REAL NOT NULL,
  price               REAL,
  gain_pct            REAL,
  pnl_sol             REAL,
  remaining_fraction  REAL,
  at_ms               REAL,
  created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_position_fills_mint ON position_fills(mint, created_at);

-- Counterfactual outcomes for candidates we did NOT trade (vetoed, or accepted
-- but not entered). We sample the post-graduation price for a bounded window and
-- record the peak the token reached, so veto quality (false positives) can be
-- measured per reason before any threshold is loosened. Never trades capital.
CREATE TABLE IF NOT EXISTS shadow_outcomes (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  mint                TEXT NOT NULL,
  verdict             TEXT NOT NULL,          -- veto | accept_not_entered
  primary_veto_code   TEXT,
  baseline_price      REAL NOT NULL,          -- price at graduation (hypothetical entry)
  peak_price          REAL,
  trough_price        REAL,
  peak_mfe_pct        REAL,                    -- (peak/baseline - 1) * 100
  max_mae_pct         REAL,                    -- (trough/baseline - 1) * 100 (<= 0)
  hit_25              INTEGER NOT NULL DEFAULT 0,
  hit_50              INTEGER NOT NULL DEFAULT 0,
  samples             INTEGER NOT NULL DEFAULT 0,
  tracked_ms          REAL,
  session_id          INTEGER,
  config_hash         TEXT,
  created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_shadow_outcomes_veto ON shadow_outcomes(primary_veto_code);
`;

export interface OpenDbOptions {
  /** Path to the sqlite file. Parent dirs are created if missing. */
  path: string;
  /** Use an in-memory DB (tests). Ignores path. */
  memory?: boolean;
}

export function openDb(opts: OpenDbOptions): DB {
  const target = opts.memory ? ':memory:' : resolve(opts.path);
  if (!opts.memory) mkdirSync(dirname(target), { recursive: true });

  const db = new DatabaseSync(target);
  // WAL is a no-op for :memory: but harmless.
  db.exec('PRAGMA journal_mode = WAL');
  db.exec('PRAGMA synchronous = NORMAL');
  db.exec('PRAGMA foreign_keys = ON');
  db.exec(SCHEMA);
  migrate(db);
  return db;
}

/** Additive, idempotent column migrations for tables created before them. */
function migrate(db: DB): void {
  addColumnIfMissing(db, 'positions', 'raw_base_amount', 'TEXT'); // actual on-chain tokens held (bigint as string)
  addColumnIfMissing(db, 'positions', 'pricing_json', 'TEXT'); // serialized PoolPricingRef, for crash recovery
  addColumnIfMissing(db, 'positions', 'execution_json', 'TEXT'); // send/confirm/reconciliation details
  addColumnIfMissing(db, 'positions', 'exit_intent_json', 'TEXT'); // durable live exit supervisor state
  addColumnIfMissing(db, 'positions', 'relaxed_risk', 'INTEGER NOT NULL DEFAULT 0');
  addColumnIfMissing(db, 'positions', 'relaxed_reasons_json', 'TEXT');
  addColumnIfMissing(db, 'positions', 'exit_price', 'REAL'); // exit price for closed positions
  addColumnIfMissing(db, 'positions', 'momentum_window_ms', 'INTEGER'); // selected early-flow window
  // Analytics denorm columns (operator monitoring + reports)
  addColumnIfMissing(db, 'positions', 'gross_pnl_sol', 'REAL');
  addColumnIfMissing(db, 'positions', 'fees_sol', 'REAL');
  addColumnIfMissing(db, 'positions', 'net_pnl_sol', 'REAL');
  addColumnIfMissing(db, 'positions', 'entry_soft_score', 'REAL');
  addColumnIfMissing(db, 'positions', 'high_volatility', 'INTEGER');
  addColumnIfMissing(db, 'positions', 'mfe_pct', 'REAL');
  addColumnIfMissing(db, 'positions', 'mae_pct', 'REAL');
  addColumnIfMissing(db, 'positions', 'hold_ms', 'REAL');
  addColumnIfMissing(db, 'positions', 'feed_source', 'TEXT');
  addColumnIfMissing(db, 'positions', 'venue', 'TEXT');
  addColumnIfMissing(db, 'positions', 'mode', 'TEXT');
  addColumnIfMissing(db, 'candidates', 'primary_veto_code', 'TEXT');
  // Strategy-week / model-ready features
  addColumnIfMissing(db, 'positions', 'session_id', 'INTEGER');
  addColumnIfMissing(db, 'positions', 'config_hash', 'TEXT');
  addColumnIfMissing(db, 'positions', 'time_to_mfe_ms', 'REAL');
  addColumnIfMissing(db, 'positions', 'time_to_mae_ms', 'REAL');
  addColumnIfMissing(db, 'positions', 'path_marks_json', 'TEXT');
  addColumnIfMissing(db, 'positions', 'left_on_table_pct', 'REAL');
  addColumnIfMissing(db, 'positions', 'detect_to_open_ms', 'REAL');
  addColumnIfMissing(db, 'positions', 'size_multiplier', 'REAL');
  addColumnIfMissing(db, 'positions', 'early_flow_net_sol', 'REAL');
  addColumnIfMissing(db, 'positions', 'early_flow_rate', 'REAL');
  addColumnIfMissing(db, 'positions', 'pool_sol_at_entry', 'REAL');
  addColumnIfMissing(db, 'positions', 'buy_impact_pct', 'REAL');
  addColumnIfMissing(db, 'positions', 'top10_share', 'REAL');
  addColumnIfMissing(db, 'positions', 'max_holder_share', 'REAL');
  addColumnIfMissing(db, 'positions', 'creator_share', 'REAL');
  addColumnIfMissing(db, 'positions', 'rugcheck_score', 'REAL');
  addColumnIfMissing(db, 'positions', 'has_socials', 'INTEGER');
  addColumnIfMissing(db, 'positions', 'score_components_json', 'TEXT');
  addColumnIfMissing(db, 'positions', 'unknowns_json', 'TEXT');
  addColumnIfMissing(db, 'positions', 'enrichment_ms', 'REAL');
  addColumnIfMissing(db, 'candidates', 'session_id', 'INTEGER');
  addColumnIfMissing(db, 'candidates', 'config_hash', 'TEXT');
  addColumnIfMissing(db, 'candidates', 'size_multiplier', 'REAL');
  addColumnIfMissing(db, 'candidates', 'early_flow_net_sol', 'REAL');
  addColumnIfMissing(db, 'candidates', 'early_flow_rate', 'REAL');
  addColumnIfMissing(db, 'candidates', 'pool_sol_at_entry', 'REAL');
  addColumnIfMissing(db, 'candidates', 'buy_impact_pct', 'REAL');
  addColumnIfMissing(db, 'candidates', 'top10_share', 'REAL');
  addColumnIfMissing(db, 'candidates', 'max_holder_share', 'REAL');
  addColumnIfMissing(db, 'candidates', 'creator_share', 'REAL');
  addColumnIfMissing(db, 'candidates', 'rugcheck_score', 'REAL');
  addColumnIfMissing(db, 'candidates', 'has_socials', 'INTEGER');
  addColumnIfMissing(db, 'candidates', 'score_components_json', 'TEXT');
  addColumnIfMissing(db, 'candidates', 'unknowns_json', 'TEXT');
  addColumnIfMissing(db, 'candidates', 'enrichment_ms', 'REAL');
  addColumnIfMissing(db, 'candidates', 'momentum_window_ms', 'INTEGER');
  addColumnIfMissing(db, 'candidates', 'relaxed_risk', 'INTEGER NOT NULL DEFAULT 0');
  addColumnIfMissing(db, 'candidates', 'relaxed_reasons_json', 'TEXT');
  addColumnIfMissing(db, 'candidates', 'sellability_reason', 'TEXT');
}

function addColumnIfMissing(db: DB, table: string, column: string, type: string): void {
  const cols = db.prepare(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>;
  if (!cols.some((c) => c.name === column)) {
    db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${type}`);
  }
}

/** Delete price ticks older than the retention window (Section 10). */
export function prunePriceTicks(db: DB, retentionDays: number): number {
  const stmt = db.prepare(
    `DELETE FROM price_ticks WHERE created_at < datetime('now', ?)`,
  );
  const info = stmt.run(`-${retentionDays} days`);
  return Number(info.changes);
}

/** Delete latency samples older than the retention window. */
export function pruneLatencySamples(db: DB, retentionDays: number): number {
  const stmt = db.prepare(
    `DELETE FROM latency_samples WHERE created_at < datetime('now', ?)`,
  );
  const info = stmt.run(`-${retentionDays} days`);
  return Number(info.changes);
}
