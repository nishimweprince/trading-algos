import type { DB } from './db.ts';
import type { CandidateVerdict, GraduationEvent, Position } from '../core/types.ts';

export type OperatorEventLevel = 'info' | 'warn' | 'error';

export interface OperatorEventInput {
  category: string;
  level: OperatorEventLevel;
  message: string;
  entityMint?: string;
  payload?: unknown;
}

/**
 * Repository layer — the only place that writes SQL. Modules depend on these
 * methods, not on the raw DB, so schema changes stay contained.
 */
export class Repositories {
  private readonly db: DB;

  constructor(db: DB) {
    this.db = db;
  }

  recordGraduation(ev: GraduationEvent): void {
    this.db
      .prepare(
        `INSERT OR IGNORE INTO graduations
           (mint, slot, detected_at_ns, feed_source, venue, pool_address, detection_latency_ms)
         VALUES (@mint, @slot, @detectedAtNs, @feedSource, @venue, @poolAddress, @latency)`,
      )
      .run({
        mint: ev.mint,
        slot: ev.slot,
        detectedAtNs: ev.receivedAtNs.toString(),
        feedSource: ev.feedSource,
        venue: ev.venue,
        poolAddress: ev.poolAddress,
        latency: ev.detectionLatencyMs ?? null,
      });
  }

  recordVerdict(v: CandidateVerdict, enrichmentJson: string | null): void {
    this.db
      .prepare(
        `INSERT INTO candidates
           (mint, enrichment_json, hard_check_results, soft_score, verdict, veto_reasons, high_volatility)
         VALUES (@mint, @enrichment, @hardChecks, @softScore, @verdict, @vetoReasons, @highVol)`,
      )
      .run({
        mint: v.mint,
        enrichment: enrichmentJson,
        hardChecks: JSON.stringify(v.hardChecks),
        softScore: v.softScore,
        verdict: v.verdict,
        vetoReasons: JSON.stringify(v.vetoReasons),
        highVol: v.highVolatility ? 1 : 0,
      });
  }

  upsertPosition(
    p: Position,
    txns: {
      entryTx?: string | undefined;
      exitTx?: string | undefined;
      rawBaseAmount?: bigint;
      pricingJson?: string | undefined;
      executionJson?: string | undefined;
      exitTriggerToConfirmMs?: number | undefined;
    } = {},
  ): void {
    // v1: positions are append-mostly; a full history row per state change is
    // acceptable for the low write rate and aids post-hoc analysis.
    this.db
      .prepare(
        `INSERT INTO positions
           (mint, entry_tx, entry_price, size_sol, state, exit_reason, exit_tx, pnl_sol, pnl_pct, opened_at, closed_at,
            raw_base_amount, pricing_json, execution_json, exit_trigger_to_confirm_ms)
         VALUES (@mint, @entryTx, @entryPrice, @sizeSol, @state, @exitReason, @exitTx, @pnlSol, @pnlPct, @openedAt, @closedAt,
                 @rawBaseAmount, @pricingJson, @executionJson, @exitTriggerToConfirmMs)`,
      )
      .run({
        mint: p.mint,
        entryTx: txns.entryTx ?? null,
        entryPrice: p.entryPrice ?? null,
        sizeSol: p.sizeSol,
        state: p.state,
        exitReason: p.exitTrigger ?? null,
        exitTx: txns.exitTx ?? null,
        pnlSol: p.pnlSol ?? null,
        pnlPct: p.pnlPct ?? null,
        openedAt: p.openedAt ? new Date(p.openedAt).toISOString() : null,
        closedAt: p.closedAt ? new Date(p.closedAt).toISOString() : null,
        rawBaseAmount: txns.rawBaseAmount !== undefined ? txns.rawBaseAmount.toString() : null,
        pricingJson: txns.pricingJson ?? null,
        executionJson: txns.executionJson ?? null,
        exitTriggerToConfirmMs: txns.exitTriggerToConfirmMs ?? null,
      });
  }

  insertPriceTick(t: { mint: string; slot: number | null; price: number; solReserve: number }): void {
    this.db
      .prepare(`INSERT INTO price_ticks (mint, slot, price, sol_reserve) VALUES (@mint, @slot, @price, @solReserve)`)
      .run({ mint: t.mint, slot: t.slot, price: t.price, solReserve: t.solReserve });
  }

  /** Sum of realized PnL for positions CLOSED at/after an ISO-UTC timestamp. */
  sumRealizedPnlSince(isoUtc: string): number {
    const row = this.db
      .prepare(`SELECT COALESCE(SUM(pnl_sol), 0) AS s FROM positions WHERE state = 'CLOSED' AND closed_at >= ?`)
      .get(isoUtc) as { s: number };
    return row.s;
  }

  /** Count CLOSED positions with a given exit trigger at/after an ISO-UTC timestamp. */
  countClosedByTriggerSince(trigger: string, isoUtc: string): number {
    const row = this.db
      .prepare(`SELECT COUNT(*) AS n FROM positions WHERE state = 'CLOSED' AND exit_reason = ? AND closed_at >= ?`)
      .get(trigger, isoUtc) as { n: number };
    return row.n;
  }

  /** Most-recent CLOSED PnLs, newest first — for consecutive-loss rehydration. */
  recentClosedPnls(limit: number): number[] {
    const rows = this.db
      .prepare(
        `SELECT pnl_sol FROM positions WHERE state = 'CLOSED' AND pnl_sol IS NOT NULL ORDER BY rowid DESC LIMIT ?`,
      )
      .all(limit) as Array<{ pnl_sol: number }>;
    return rows.map((r) => r.pnl_sol);
  }

  /**
   * Positions whose LATEST row (by rowid) is state OPEN — i.e. open at restart.
   * Used by crash recovery to reconcile against chain state.
   */
  latestOpenPositions(): Array<{
    mint: string;
    entryPrice: number | null;
    sizeSol: number;
    openedAt: string | null;
    rawBaseAmount: string | null;
    pricingJson: string | null;
    executionJson: string | null;
  }> {
    const rows = this.db
      .prepare(
        `SELECT p.mint, p.entry_price AS entryPrice, p.size_sol AS sizeSol, p.opened_at AS openedAt,
                p.raw_base_amount AS rawBaseAmount, p.pricing_json AS pricingJson,
                p.execution_json AS executionJson
           FROM positions p
           JOIN (SELECT mint, MAX(rowid) AS mx FROM positions GROUP BY mint) latest
             ON p.mint = latest.mint AND p.rowid = latest.mx
          WHERE p.state = 'OPEN'`,
      )
      .all() as Array<{
      mint: string;
      entryPrice: number | null;
      sizeSol: number;
      openedAt: string | null;
      rawBaseAmount: string | null;
      pricingJson: string | null;
      executionJson: string | null;
    }>;
    return rows;
  }

  recordBreakerEvent(type: string, tripped: boolean, detail?: string): void {
    this.db
      .prepare(`INSERT INTO breaker_events (type, detail, tripped) VALUES (?, ?, ?)`)
      .run(type, detail ?? null, tripped ? 1 : 0);
  }

  recordOperatorEvent(event: OperatorEventInput): number {
    const info = this.db
      .prepare(
        `INSERT INTO operator_events
           (category, level, message, entity_mint, payload_json)
         VALUES (@category, @level, @message, @entityMint, @payload)`,
      )
      .run({
        category: event.category,
        level: event.level,
        message: event.message,
        entityMint: event.entityMint ?? null,
        payload: event.payload === undefined ? null : JSON.stringify(event.payload, jsonReplacer),
      });
    return Number(info.lastInsertRowid);
  }

  isCreatorBlacklisted(address: string): boolean {
    const row = this.db
      .prepare(`SELECT 1 FROM blacklisted_creators WHERE address = ?`)
      .get(address);
    return row !== undefined;
  }

  isMintBlacklisted(mint: string): boolean {
    const row = this.db.prepare(`SELECT 1 FROM blacklisted_mints WHERE mint = ?`).get(mint);
    return row !== undefined;
  }

  blacklistCreator(address: string, reason: string): void {
    this.db
      .prepare(
        `INSERT OR REPLACE INTO blacklisted_creators (address, reason) VALUES (?, ?)`,
      )
      .run(address, reason);
  }

  blacklistMint(mint: string, reason: string): void {
    this.db
      .prepare(`INSERT OR REPLACE INTO blacklisted_mints (mint, reason) VALUES (?, ?)`)
      .run(mint, reason);
  }

  countGraduations(): number {
    const row = this.db.prepare(`SELECT COUNT(*) AS n FROM graduations`).get() as { n: number };
    return row.n;
  }
}

function jsonReplacer(_key: string, value: unknown): unknown {
  if (typeof value === 'bigint') return value.toString();
  return value;
}
