import type { DB } from './db.ts';
import type { CandidateVerdict, GraduationEvent, Position } from '../core/types.ts';

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

  upsertPosition(p: Position, txns: { entryTx?: string; exitTx?: string } = {}): void {
    // v1: positions are append-mostly; a full history row per state change is
    // acceptable for the low write rate and aids post-hoc analysis.
    this.db
      .prepare(
        `INSERT INTO positions
           (mint, entry_tx, entry_price, size_sol, state, exit_reason, exit_tx, pnl_sol, pnl_pct, opened_at, closed_at)
         VALUES (@mint, @entryTx, @entryPrice, @sizeSol, @state, @exitReason, @exitTx, @pnlSol, @pnlPct, @openedAt, @closedAt)`,
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
      });
  }

  recordBreakerEvent(type: string, tripped: boolean, detail?: string): void {
    this.db
      .prepare(`INSERT INTO breaker_events (type, detail, tripped) VALUES (?, ?, ?)`)
      .run(type, detail ?? null, tripped ? 1 : 0);
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
