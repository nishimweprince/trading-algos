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

/** Strategy-week feature columns shared by candidates + positions. */
export interface StrategyFeatureFields {
  sessionId?: number | null | undefined;
  configHash?: string | null | undefined;
  sizeMultiplier?: number | null | undefined;
  earlyFlowNetSol?: number | null | undefined;
  earlyFlowRate?: number | null | undefined;
  poolSolAtEntry?: number | null | undefined;
  buyImpactPct?: number | null | undefined;
  top10Share?: number | null | undefined;
  maxHolderShare?: number | null | undefined;
  creatorShare?: number | null | undefined;
  rugcheckScore?: number | null | undefined;
  hasSocials?: boolean | null | undefined;
  scoreComponentsJson?: string | null | undefined;
  unknownsJson?: string | null | undefined;
  enrichmentMs?: number | null | undefined;
  momentumWindowMs?: number | null | undefined;
  relaxedRisk?: boolean | null | undefined;
  relaxedReasonsJson?: string | null | undefined;
  sellabilityReason?: string | null | undefined;
}

export type PositionTxnFields = StrategyFeatureFields & {
  entryTx?: string | null | undefined;
  exitTx?: string | null | undefined;
  exitPrice?: number | null | undefined;
  rawBaseAmount?: bigint | undefined;
  pricingJson?: string | null | undefined;
  executionJson?: string | null | undefined;
  exitIntentJson?: string | null | undefined;
  exitTriggerToConfirmMs?: number | null | undefined;
  grossPnlSol?: number | null | undefined;
  feesSol?: number | null | undefined;
  netPnlSol?: number | null | undefined;
  entrySoftScore?: number | null | undefined;
  highVolatility?: boolean | null | undefined;
  mfePct?: number | null | undefined;
  maePct?: number | null | undefined;
  holdMs?: number | null | undefined;
  feedSource?: string | null | undefined;
  venue?: string | null | undefined;
  mode?: string | null | undefined;
  timeToMfeMs?: number | null | undefined;
  timeToMaeMs?: number | null | undefined;
  pathMarksJson?: string | null | undefined;
  leftOnTablePct?: number | null | undefined;
  detectToOpenMs?: number | null | undefined;
};

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

  recordVerdict(
    v: CandidateVerdict,
    enrichmentJson: string | null,
    features: StrategyFeatureFields = {},
  ): void {
    const primaryVeto = v.vetoReasons[0] ?? null;
    this.db
      .prepare(
        `INSERT INTO candidates
           (mint, enrichment_json, hard_check_results, soft_score, verdict, veto_reasons, high_volatility,
            relaxed_risk, relaxed_reasons_json, sellability_reason, primary_veto_code,
            session_id, config_hash, size_multiplier, early_flow_net_sol, early_flow_rate, pool_sol_at_entry, buy_impact_pct,
            top10_share, max_holder_share, creator_share, rugcheck_score, has_socials, score_components_json,
            unknowns_json, enrichment_ms, momentum_window_ms)
         VALUES (@mint, @enrichment, @hardChecks, @softScore, @verdict, @vetoReasons, @highVol,
            @relaxedRisk, @relaxedReasonsJson, @sellabilityReason, @primaryVeto,
            @sessionId, @configHash, @sizeMultiplier, @earlyFlowNetSol, @earlyFlowRate, @poolSolAtEntry, @buyImpactPct,
            @top10Share, @maxHolderShare, @creatorShare, @rugcheckScore, @hasSocials, @scoreComponentsJson,
            @unknownsJson, @enrichmentMs, @momentumWindowMs)`,
      )
      .run({
        mint: v.mint,
        enrichment: enrichmentJson,
        hardChecks: JSON.stringify(v.hardChecks),
        softScore: v.softScore,
        verdict: v.verdict,
        vetoReasons: JSON.stringify(v.vetoReasons),
        highVol: v.highVolatility ? 1 : 0,
        relaxedRisk: v.relaxedRisk ? 1 : 0,
        relaxedReasonsJson: v.relaxedReasons?.length ? JSON.stringify(v.relaxedReasons) : null,
        sellabilityReason: features.sellabilityReason ?? v.hardChecks.find((c) => c.id === 'H4')?.reason ?? null,
        primaryVeto,
        sessionId: features.sessionId ?? null,
        configHash: features.configHash ?? null,
        sizeMultiplier: features.sizeMultiplier ?? v.sizeMultiplier,
        earlyFlowNetSol: features.earlyFlowNetSol ?? null,
        earlyFlowRate: features.earlyFlowRate ?? null,
        poolSolAtEntry: features.poolSolAtEntry ?? null,
        buyImpactPct: features.buyImpactPct ?? null,
        top10Share: features.top10Share ?? null,
        maxHolderShare: features.maxHolderShare ?? null,
        creatorShare: features.creatorShare ?? null,
        rugcheckScore: features.rugcheckScore ?? null,
        hasSocials: features.hasSocials === null || features.hasSocials === undefined ? null : features.hasSocials ? 1 : 0,
        scoreComponentsJson:
          features.scoreComponentsJson ??
          (v.scoreComponents ? JSON.stringify(v.scoreComponents) : null),
        unknownsJson: features.unknownsJson ?? null,
        enrichmentMs: features.enrichmentMs ?? null,
        momentumWindowMs: features.momentumWindowMs ?? null,
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

  upsertPosition(p: Position, txns: PositionTxnFields = {}): void {
    // v1: positions are append-mostly; a full history row per state change is
    // acceptable for the low write rate and aids post-hoc analysis.
    this.db
      .prepare(
        `INSERT INTO positions
           (mint, entry_tx, entry_price, exit_price, size_sol, state, exit_reason, exit_tx, pnl_sol, pnl_pct, opened_at, closed_at,
            raw_base_amount, pricing_json, execution_json, exit_intent_json, relaxed_risk, relaxed_reasons_json,
            exit_trigger_to_confirm_ms, momentum_window_ms,
            gross_pnl_sol, fees_sol, net_pnl_sol, entry_soft_score, high_volatility, mfe_pct, mae_pct, hold_ms,
            feed_source, venue, mode, session_id, config_hash, time_to_mfe_ms, time_to_mae_ms, path_marks_json,
            left_on_table_pct, detect_to_open_ms, size_multiplier, early_flow_net_sol, early_flow_rate, pool_sol_at_entry,
            buy_impact_pct, top10_share, max_holder_share, creator_share, rugcheck_score, has_socials,
            score_components_json, unknowns_json, enrichment_ms)
         VALUES (@mint, @entryTx, @entryPrice, @exitPrice, @sizeSol, @state, @exitReason, @exitTx, @pnlSol, @pnlPct, @openedAt, @closedAt,
                 @rawBaseAmount, @pricingJson, @executionJson, @exitIntentJson, @relaxedRisk, @relaxedReasonsJson,
                 @exitTriggerToConfirmMs, @momentumWindowMs,
                 @grossPnlSol, @feesSol, @netPnlSol, @entrySoftScore, @highVolatility, @mfePct, @maePct, @holdMs,
                 @feedSource, @venue, @mode, @sessionId, @configHash, @timeToMfeMs, @timeToMaeMs, @pathMarksJson,
                 @leftOnTablePct, @detectToOpenMs, @sizeMultiplier, @earlyFlowNetSol, @earlyFlowRate, @poolSolAtEntry,
                 @buyImpactPct, @top10Share, @maxHolderShare, @creatorShare, @rugcheckScore, @hasSocials,
                 @scoreComponentsJson, @unknownsJson, @enrichmentMs)`,
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
        relaxedRisk: txns.relaxedRisk ? 1 : 0,
        relaxedReasonsJson: txns.relaxedReasonsJson ?? null,
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
        sessionId: txns.sessionId ?? null,
        configHash: txns.configHash ?? null,
        timeToMfeMs: txns.timeToMfeMs ?? null,
        timeToMaeMs: txns.timeToMaeMs ?? null,
        pathMarksJson: txns.pathMarksJson ?? null,
        leftOnTablePct: txns.leftOnTablePct ?? null,
        detectToOpenMs: txns.detectToOpenMs ?? null,
        sizeMultiplier: txns.sizeMultiplier ?? null,
        earlyFlowNetSol: txns.earlyFlowNetSol ?? null,
        earlyFlowRate: txns.earlyFlowRate ?? null,
        poolSolAtEntry: txns.poolSolAtEntry ?? null,
        buyImpactPct: txns.buyImpactPct ?? null,
        top10Share: txns.top10Share ?? null,
        maxHolderShare: txns.maxHolderShare ?? null,
        creatorShare: txns.creatorShare ?? null,
        rugcheckScore: txns.rugcheckScore ?? null,
        hasSocials: txns.hasSocials === null || txns.hasSocials === undefined ? null : txns.hasSocials ? 1 : 0,
        scoreComponentsJson: txns.scoreComponentsJson ?? null,
        unknownsJson: txns.unknownsJson ?? null,
        enrichmentMs: txns.enrichmentMs ?? null,
      });
  }

  startRunSession(input: {
    mode: string;
    configHash: string;
    configJson: string;
    gitCommit?: string | null;
  }): number {
    const info = this.db
      .prepare(
        `INSERT INTO run_sessions (mode, config_hash, config_json, git_commit)
         VALUES (@mode, @configHash, @configJson, @gitCommit)`,
      )
      .run({
        mode: input.mode,
        configHash: input.configHash,
        configJson: input.configJson,
        gitCommit: input.gitCommit ?? null,
      });
    return Number(info.lastInsertRowid);
  }

  endRunSession(sessionId: number): void {
    this.db
      .prepare(`UPDATE run_sessions SET ended_at = datetime('now') WHERE id = ? AND ended_at IS NULL`)
      .run(sessionId);
  }

  recordPositionFill(fill: {
    mint: string;
    sessionId?: number | null;
    trigger: string;
    fraction: number;
    price?: number;
    gainPct?: number;
    pnlSol?: number;
    remainingFraction?: number;
    atMs?: number;
  }): void {
    this.db
      .prepare(
        `INSERT INTO position_fills
           (mint, session_id, trigger, fraction, price, gain_pct, pnl_sol, remaining_fraction, at_ms)
         VALUES (@mint, @sessionId, @trigger, @fraction, @price, @gainPct, @pnlSol, @remainingFraction, @atMs)`,
      )
      .run({
        mint: fill.mint,
        sessionId: fill.sessionId ?? null,
        trigger: fill.trigger,
        fraction: fill.fraction,
        price: fill.price ?? null,
        gainPct: fill.gainPct ?? null,
        pnlSol: fill.pnlSol ?? null,
        remainingFraction: fill.remainingFraction ?? null,
        atMs: fill.atMs ?? null,
      });
  }

  latestCandidateFeatures(mint: string): StrategyFeatureFields & { softScore: number | null; highVolatility: boolean | null } {
    const row = this.db
      .prepare(
        `SELECT soft_score, high_volatility, relaxed_risk, relaxed_reasons_json, sellability_reason,
                session_id, config_hash, size_multiplier, early_flow_net_sol, early_flow_rate,
                pool_sol_at_entry, buy_impact_pct, top10_share, max_holder_share, creator_share, rugcheck_score,
                has_socials, score_components_json, unknowns_json, enrichment_ms, momentum_window_ms
         FROM candidates WHERE mint = ? ORDER BY rowid DESC LIMIT 1`,
      )
      .get(mint) as
      | {
          soft_score: number | null;
          high_volatility: number | null;
          relaxed_risk: number | null;
          relaxed_reasons_json: string | null;
          sellability_reason: string | null;
          session_id: number | null;
          config_hash: string | null;
          size_multiplier: number | null;
          early_flow_net_sol: number | null;
          early_flow_rate: number | null;
          pool_sol_at_entry: number | null;
          buy_impact_pct: number | null;
          top10_share: number | null;
          max_holder_share: number | null;
          creator_share: number | null;
          rugcheck_score: number | null;
          has_socials: number | null;
          score_components_json: string | null;
          unknowns_json: string | null;
          enrichment_ms: number | null;
          momentum_window_ms: number | null;
        }
      | undefined;
    if (!row) {
      return { softScore: null, highVolatility: null };
    }
    return {
      softScore: row.soft_score,
      highVolatility: row.high_volatility === null ? null : row.high_volatility === 1,
      relaxedRisk: row.relaxed_risk === null ? null : row.relaxed_risk === 1,
      relaxedReasonsJson: row.relaxed_reasons_json,
      sellabilityReason: row.sellability_reason,
      sessionId: row.session_id,
      configHash: row.config_hash,
      sizeMultiplier: row.size_multiplier,
      earlyFlowNetSol: row.early_flow_net_sol,
      earlyFlowRate: row.early_flow_rate,
      poolSolAtEntry: row.pool_sol_at_entry,
      buyImpactPct: row.buy_impact_pct,
      top10Share: row.top10_share,
      maxHolderShare: row.max_holder_share,
      creatorShare: row.creator_share,
      rugcheckScore: row.rugcheck_score,
      hasSocials: row.has_socials === null ? null : row.has_socials === 1,
      scoreComponentsJson: row.score_components_json,
      unknownsJson: row.unknowns_json,
      enrichmentMs: row.enrichment_ms,
      momentumWindowMs: row.momentum_window_ms,
    };
  }

  graduationReceivedAtNs(mint: string): bigint | null {
    const row = this.db
      .prepare(`SELECT detected_at_ns FROM graduations WHERE mint = ? ORDER BY rowid DESC LIMIT 1`)
      .get(mint) as { detected_at_ns: string } | undefined;
    if (!row?.detected_at_ns) return null;
    try {
      return BigInt(row.detected_at_ns);
    } catch {
      return null;
    }
  }

  /** Wall-clock created_at of latest graduation for detect→open latency. */
  graduationCreatedAtMs(mint: string): number | null {
    const row = this.db
      .prepare(`SELECT created_at FROM graduations WHERE mint = ? ORDER BY rowid DESC LIMIT 1`)
      .get(mint) as { created_at: string } | undefined;
    if (!row?.created_at) return null;
    const raw = row.created_at;
    const parsed = Date.parse(raw.includes('T') ? raw : `${raw.replace(' ', 'T')}Z`);
    return Number.isFinite(parsed) ? parsed : null;
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

  /** Persist a counterfactual outcome for a candidate we did not trade. */
  recordShadowOutcome(o: {
    mint: string;
    verdict: 'veto' | 'accept_not_entered';
    primaryVetoCode: string | null;
    baselinePrice: number;
    peakPrice: number;
    troughPrice: number;
    peakMfePct: number;
    maxMaePct: number;
    hit25: boolean;
    hit50: boolean;
    samples: number;
    trackedMs: number;
    sessionId?: number | null;
    configHash?: string | null;
  }): void {
    this.db
      .prepare(
        `INSERT INTO shadow_outcomes
           (mint, verdict, primary_veto_code, baseline_price, peak_price, trough_price,
            peak_mfe_pct, max_mae_pct, hit_25, hit_50, samples, tracked_ms, session_id, config_hash)
         VALUES (@mint, @verdict, @primaryVetoCode, @baselinePrice, @peakPrice, @troughPrice,
            @peakMfePct, @maxMaePct, @hit25, @hit50, @samples, @trackedMs, @sessionId, @configHash)`,
      )
      .run({
        mint: o.mint,
        verdict: o.verdict,
        primaryVetoCode: o.primaryVetoCode,
        baselinePrice: o.baselinePrice,
        peakPrice: o.peakPrice,
        troughPrice: o.troughPrice,
        peakMfePct: o.peakMfePct,
        maxMaePct: o.maxMaePct,
        hit25: o.hit25 ? 1 : 0,
        hit50: o.hit50 ? 1 : 0,
        samples: o.samples,
        trackedMs: o.trackedMs,
        sessionId: o.sessionId ?? null,
        configHash: o.configHash ?? null,
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
    relaxedRisk: number | null;
    relaxedReasonsJson: string | null;
  }> {
    const rows = this.db
      .prepare(
        `SELECT p.mint, p.entry_price AS entryPrice, p.size_sol AS sizeSol, p.opened_at AS openedAt,
                p.entry_tx AS entryTx, p.exit_tx AS exitTx,
                p.raw_base_amount AS rawBaseAmount, p.pricing_json AS pricingJson,
                p.execution_json AS executionJson, p.exit_intent_json AS exitIntentJson,
                p.momentum_window_ms AS momentumWindowMs,
                p.relaxed_risk AS relaxedRisk, p.relaxed_reasons_json AS relaxedReasonsJson
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
      relaxedRisk: number | null;
      relaxedReasonsJson: string | null;
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
    relaxedRisk: number | null;
    relaxedReasonsJson: string | null;
  }> {
    const rows = this.db
      .prepare(
        `SELECT p.mint, p.entry_price AS entryPrice, p.size_sol AS sizeSol, p.opened_at AS openedAt,
                p.entry_tx AS entryTx, p.exit_tx AS exitTx,
                p.raw_base_amount AS rawBaseAmount, p.pricing_json AS pricingJson,
                p.execution_json AS executionJson, p.exit_intent_json AS exitIntentJson,
                p.momentum_window_ms AS momentumWindowMs,
                p.relaxed_risk AS relaxedRisk, p.relaxed_reasons_json AS relaxedReasonsJson
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
      relaxedRisk: number | null;
      relaxedReasonsJson: string | null;
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
