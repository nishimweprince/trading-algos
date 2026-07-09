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

export interface AnalyticsSnapshotInput {
  period: 'hour' | 'day';
  periodStart: string;
  mode: string;
  realizedPnlSol?: number;
  tradeCount?: number;
  wins?: number;
  losses?: number;
  expectancySol?: number;
  profitFactor?: number;
  maxDrawdownSol?: number;
  graduations?: number;
  accepted?: number;
  vetoed?: number;
  entered?: number;
  failed?: number;
  emergencyExits?: number;
  detectionP50Ms?: number;
  detectionP95Ms?: number;
  exitConfirmP50Ms?: number;
  exitConfirmP95Ms?: number;
  feesSol?: number;
  payloadJson?: string;
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
    const primaryVeto = v.vetoReasons[0] ?? null;
    this.db
      .prepare(
        `INSERT INTO candidates
           (mint, enrichment_json, hard_check_results, soft_score, verdict, veto_reasons, high_volatility, primary_veto_code)
         VALUES (@mint, @enrichment, @hardChecks, @softScore, @verdict, @vetoReasons, @highVol, @primaryVeto)`,
      )
      .run({
        mint: v.mint,
        enrichment: enrichmentJson,
        hardChecks: JSON.stringify(v.hardChecks),
        softScore: v.softScore,
        verdict: v.verdict,
        vetoReasons: JSON.stringify(v.vetoReasons),
        highVol: v.highVolatility ? 1 : 0,
        primaryVeto,
      });

    if (v.hardChecks.length > 0) {
      const insertCheck = this.db.prepare(
        `INSERT INTO candidate_check_results (mint, check_id, status, detail)
         VALUES (@mint, @checkId, @status, @detail)`,
      );
      for (const check of v.hardChecks) {
        insertCheck.run({
          mint: v.mint,
          checkId: check.id,
          status: check.status,
          detail: check.detail ?? null,
        });
      }
    }
  }

  upsertPosition(
    p: Position,
    txns: {
      entryTx?: string | undefined;
      exitTx?: string | undefined;
      exitPrice?: number | undefined;
      rawBaseAmount?: bigint;
      pricingJson?: string | undefined;
      executionJson?: string | undefined;
      exitIntentJson?: string | undefined;
      exitTriggerToConfirmMs?: number | undefined;
      momentumWindowMs?: number | undefined;
      grossPnlSol?: number | undefined;
      feesSol?: number | undefined;
      netPnlSol?: number | undefined;
      entrySoftScore?: number | undefined;
      highVolatility?: boolean | undefined;
      mfePct?: number | undefined;
      maePct?: number | undefined;
      holdMs?: number | undefined;
      feedSource?: string | undefined;
      venue?: string | undefined;
      mode?: string | undefined;
    } = {},
  ): void {
    // v1: positions are append-mostly; a full history row per state change is
    // acceptable for the low write rate and aids post-hoc analysis.
    this.db
      .prepare(
        `INSERT INTO positions
           (mint, entry_tx, entry_price, exit_price, size_sol, state, exit_reason, exit_tx, pnl_sol, pnl_pct, opened_at, closed_at,
            raw_base_amount, pricing_json, execution_json, exit_intent_json, exit_trigger_to_confirm_ms, momentum_window_ms,
            gross_pnl_sol, fees_sol, net_pnl_sol, entry_soft_score, high_volatility, mfe_pct, mae_pct, hold_ms,
            feed_source, venue, mode)
         VALUES (@mint, @entryTx, @entryPrice, @exitPrice, @sizeSol, @state, @exitReason, @exitTx, @pnlSol, @pnlPct, @openedAt, @closedAt,
                 @rawBaseAmount, @pricingJson, @executionJson, @exitIntentJson, @exitTriggerToConfirmMs, @momentumWindowMs,
                 @grossPnlSol, @feesSol, @netPnlSol, @entrySoftScore, @highVolatility, @mfePct, @maePct, @holdMs,
                 @feedSource, @venue, @mode)`,
      )
      .run({
        mint: p.mint,
        entryTx: txns.entryTx ?? null,
        entryPrice: p.entryPrice ?? null,
        exitPrice: txns.exitPrice ?? null,
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
        exitIntentJson: txns.exitIntentJson ?? null,
        exitTriggerToConfirmMs: txns.exitTriggerToConfirmMs ?? null,
        momentumWindowMs: txns.momentumWindowMs ?? null,
        grossPnlSol: txns.grossPnlSol ?? null,
        feesSol: txns.feesSol ?? null,
        netPnlSol: txns.netPnlSol ?? p.pnlSol ?? null,
        entrySoftScore: txns.entrySoftScore ?? null,
        highVolatility: txns.highVolatility === undefined ? null : txns.highVolatility ? 1 : 0,
        mfePct: txns.mfePct ?? null,
        maePct: txns.maePct ?? null,
        holdMs: txns.holdMs ?? null,
        feedSource: txns.feedSource ?? null,
        venue: txns.venue ?? null,
        mode: txns.mode ?? null,
      });
  }

  recordLatencySample(sample: {
    kind: 'detection' | 'exit_confirm' | 'entry_confirm';
    latencyMs: number;
    mint?: string;
    feedSource?: string;
  }): void {
    this.db
      .prepare(
        `INSERT INTO latency_samples (kind, mint, feed_source, latency_ms)
         VALUES (@kind, @mint, @feedSource, @latencyMs)`,
      )
      .run({
        kind: sample.kind,
        mint: sample.mint ?? null,
        feedSource: sample.feedSource ?? null,
        latencyMs: sample.latencyMs,
      });
  }

  /** Latest candidate soft score for a mint (for denorm on open/close). */
  latestSoftScore(mint: string): number | null {
    const row = this.db
      .prepare(
        `SELECT soft_score FROM candidates WHERE mint = ? ORDER BY rowid DESC LIMIT 1`,
      )
      .get(mint) as { soft_score: number | null } | undefined;
    return row?.soft_score ?? null;
  }

  /** Latest graduation feed/venue for a mint. */
  latestGraduationMeta(mint: string): { feedSource: string | null; venue: string | null } {
    const row = this.db
      .prepare(
        `SELECT feed_source AS feedSource, venue FROM graduations WHERE mint = ? ORDER BY rowid DESC LIMIT 1`,
      )
      .get(mint) as { feedSource: string | null; venue: string | null } | undefined;
    return row ?? { feedSource: null, venue: null };
  }

  upsertAnalyticsSnapshot(row: AnalyticsSnapshotInput): void {
    this.db
      .prepare(
        `INSERT INTO analytics_snapshots (
           period, period_start, mode, realized_pnl_sol, trade_count, wins, losses,
           expectancy_sol, profit_factor, max_drawdown_sol, graduations, accepted, vetoed,
           entered, failed, emergency_exits, detection_p50_ms, detection_p95_ms,
           exit_confirm_p50_ms, exit_confirm_p95_ms, fees_sol, payload_json
         ) VALUES (
           @period, @periodStart, @mode, @realizedPnlSol, @tradeCount, @wins, @losses,
           @expectancySol, @profitFactor, @maxDrawdownSol, @graduations, @accepted, @vetoed,
           @entered, @failed, @emergencyExits, @detectionP50Ms, @detectionP95Ms,
           @exitConfirmP50Ms, @exitConfirmP95Ms, @feesSol, @payloadJson
         )
         ON CONFLICT(period, period_start, mode) DO UPDATE SET
           realized_pnl_sol = excluded.realized_pnl_sol,
           trade_count = excluded.trade_count,
           wins = excluded.wins,
           losses = excluded.losses,
           expectancy_sol = excluded.expectancy_sol,
           profit_factor = excluded.profit_factor,
           max_drawdown_sol = excluded.max_drawdown_sol,
           graduations = excluded.graduations,
           accepted = excluded.accepted,
           vetoed = excluded.vetoed,
           entered = excluded.entered,
           failed = excluded.failed,
           emergency_exits = excluded.emergency_exits,
           detection_p50_ms = excluded.detection_p50_ms,
           detection_p95_ms = excluded.detection_p95_ms,
           exit_confirm_p50_ms = excluded.exit_confirm_p50_ms,
           exit_confirm_p95_ms = excluded.exit_confirm_p95_ms,
           fees_sol = excluded.fees_sol,
           payload_json = excluded.payload_json`,
      )
      .run({
        period: row.period,
        periodStart: row.periodStart,
        mode: row.mode,
        realizedPnlSol: row.realizedPnlSol ?? null,
        tradeCount: row.tradeCount ?? null,
        wins: row.wins ?? null,
        losses: row.losses ?? null,
        expectancySol: row.expectancySol ?? null,
        profitFactor: row.profitFactor ?? null,
        maxDrawdownSol: row.maxDrawdownSol ?? null,
        graduations: row.graduations ?? null,
        accepted: row.accepted ?? null,
        vetoed: row.vetoed ?? null,
        entered: row.entered ?? null,
        failed: row.failed ?? null,
        emergencyExits: row.emergencyExits ?? null,
        detectionP50Ms: row.detectionP50Ms ?? null,
        detectionP95Ms: row.detectionP95Ms ?? null,
        exitConfirmP50Ms: row.exitConfirmP50Ms ?? null,
        exitConfirmP95Ms: row.exitConfirmP95Ms ?? null,
        feesSol: row.feesSol ?? null,
        payloadJson: row.payloadJson ?? null,
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

  /** Most-recent CLOSED PnLs with timestamps, newest first. */
  recentClosedPnlRecords(limit: number): Array<{ pnlSol: number; closedAt: string | null; createdAt: string }> {
    return this.db
      .prepare(
        `SELECT pnl_sol AS pnlSol, closed_at AS closedAt, created_at AS createdAt
           FROM positions
          WHERE state = 'CLOSED' AND pnl_sol IS NOT NULL
          ORDER BY rowid DESC
          LIMIT ?`,
      )
      .all(limit) as Array<{ pnlSol: number; closedAt: string | null; createdAt: string }>;
  }

  /**
   * Positions whose LATEST row (by rowid) is state OPEN — i.e. open at restart.
   * Used by crash recovery to reconcile against chain state.
   */
  latestOpenPositions(): Array<{
    mint: string;
    entryTx: string | null;
    exitTx: string | null;
    entryPrice: number | null;
    sizeSol: number;
    openedAt: string | null;
    rawBaseAmount: string | null;
    pricingJson: string | null;
    executionJson: string | null;
    exitIntentJson: string | null;
    momentumWindowMs: number | null;
  }> {
    const rows = this.db
      .prepare(
        `SELECT p.mint, p.entry_price AS entryPrice, p.size_sol AS sizeSol, p.opened_at AS openedAt,
                p.entry_tx AS entryTx, p.exit_tx AS exitTx,
                p.raw_base_amount AS rawBaseAmount, p.pricing_json AS pricingJson,
                p.execution_json AS executionJson, p.exit_intent_json AS exitIntentJson,
                p.momentum_window_ms AS momentumWindowMs
           FROM positions p
           JOIN (SELECT mint, MAX(rowid) AS mx FROM positions GROUP BY mint) latest
             ON p.mint = latest.mint AND p.rowid = latest.mx
          WHERE p.state = 'OPEN'`,
      )
      .all() as Array<{
      mint: string;
      entryTx: string | null;
      exitTx: string | null;
      entryPrice: number | null;
      sizeSol: number;
      openedAt: string | null;
      rawBaseAmount: string | null;
      pricingJson: string | null;
      executionJson: string | null;
      exitIntentJson: string | null;
      momentumWindowMs: number | null;
    }>;
    return rows;
  }

  latestExitingPositions(): Array<{
    mint: string;
    entryTx: string | null;
    exitTx: string | null;
    entryPrice: number | null;
    sizeSol: number;
    openedAt: string | null;
    rawBaseAmount: string | null;
    pricingJson: string | null;
    executionJson: string | null;
    exitIntentJson: string | null;
    momentumWindowMs: number | null;
  }> {
    const rows = this.db
      .prepare(
        `SELECT p.mint, p.entry_price AS entryPrice, p.size_sol AS sizeSol, p.opened_at AS openedAt,
                p.entry_tx AS entryTx, p.exit_tx AS exitTx,
                p.raw_base_amount AS rawBaseAmount, p.pricing_json AS pricingJson,
                p.execution_json AS executionJson, p.exit_intent_json AS exitIntentJson,
                p.momentum_window_ms AS momentumWindowMs
           FROM positions p
           JOIN (SELECT mint, MAX(rowid) AS mx FROM positions GROUP BY mint) latest
             ON p.mint = latest.mint AND p.rowid = latest.mx
          WHERE p.state = 'EXITING'`,
      )
      .all() as Array<{
      mint: string;
      entryTx: string | null;
      exitTx: string | null;
      entryPrice: number | null;
      sizeSol: number;
      openedAt: string | null;
      rawBaseAmount: string | null;
      pricingJson: string | null;
      executionJson: string | null;
      exitIntentJson: string | null;
      momentumWindowMs: number | null;
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
