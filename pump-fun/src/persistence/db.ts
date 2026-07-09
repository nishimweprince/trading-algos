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
  created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_candidates_mint ON candidates(mint);

CREATE TABLE IF NOT EXISTS positions (
  mint                        TEXT NOT NULL,
  entry_tx                    TEXT,
  entry_price                 REAL,
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
