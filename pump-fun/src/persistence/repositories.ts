import type { DB } from './db.ts';
import type { CandidateVerdict, FeedLaunch, GraduationEvent, LiveStatus, Position } from '../core/types.ts';

/**
 * latency_samples.kind. The `*_slots` kinds store a slot count in
 * `latency_ms` (chain-relative: detection vs migration slot, submission vs
 * landed slot) — percentile queries work unchanged.
 */
export type LatencyKind =
  | 'detection'
  | 'exit_confirm'
  | 'entry_confirm'
  | 'detection_slots'
  | 'entry_land_slots'
  | 'exit_land_slots';

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
  sellabilityTxBytes?: number | null | undefined;
  sellabilityUsedLookupTable?: boolean | null | undefined;
  /** Export-completeness columns (work plan P0.5 / F15). */
  sellabilityStatus?: string | null | undefined;
  poolMovePct?: number | null | undefined;
  mintAgeMs?: number | null | undefined;
  creator?: string | null | undefined;
  mcapSolAtEntry?: number | null | undefined;
  feeTierBps?: number | null | undefined;
  populationOk?: boolean | null | undefined;
  /** P3: early-flow tx stats + manipulation features, one JSON object. */
  featuresJson?: string | null | undefined;
  /** P3.4 learned filter: model version and P(profitable after costs). */
  modelVersion?: string | null | undefined;
  modelProb?: number | null | undefined;
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
  /** Modelled constant-product impact (paper/twin); already inside feesSol. */
  slippageSol?: number | null | undefined;
  /** Usable price ticks the exit FSM evaluated over this position's life. */
  ticksObserved?: number | null | undefined;
  /** Ticks rejected as non-finite / <= 0, which never reached the FSM. */
  suspectTicks?: number | null | undefined;
  /** Entry -> first usable tick, in ms. The blind-window measurement. */
  firstTickMs?: number | null | undefined;
  /** Mid move from graduation detection to the actual fill, in percent. */
  entryMoveFromDetectPct?: number | null | undefined;
  /** 1 when fills/latency on this row came from the honest simulator, not a chain confirm. */
  simulated?: boolean | null | undefined;
};

export type DryRunCoverageKind =
  | 'eligible'
  | 'started'
  | 'skipped_missing_pricing'
  | 'dropped_capacity'
  | 'duplicate';

/**
 * One dry-run twin row. Timestamps are epoch ms in; the repository serialises
 * them to ISO text so the column matches `positions.opened_at` / `closed_at`
 * and the same `julianday(...)` range predicates work on both tables.
 */
export interface DryRunPositionInput {
  mint: string;
  state: 'OPEN' | 'CLOSED';
  liveStatus: LiveStatus;
  liveStatusDetail?: string | null | undefined;
  /** ms from twin open to the live signal arriving — validates the correlation. */
  liveStatusAtMs?: number | null | undefined;
  sizeSol?: number | null | undefined;
  entryPrice?: number | null | undefined;
  exitPrice?: number | null | undefined;
  exitReason?: string | null | undefined;
  openedAt?: number | null | undefined;
  closedAt?: number | null | undefined;
  grossPnlSol?: number | null | undefined;
  feesSol?: number | null | undefined;
  netPnlSol?: number | null | undefined;
  pnlPct?: number | null | undefined;
  mfePct?: number | null | undefined;
  maePct?: number | null | undefined;
  timeToMfeMs?: number | null | undefined;
  timeToMaeMs?: number | null | undefined;
  holdMs?: number | null | undefined;
  fillCount?: number | undefined;
  samples?: number | undefined;
  highVolatility?: boolean | undefined;
  relaxedRisk?: boolean | undefined;
  detectToOpenMs?: number | null | undefined;
  sessionId?: number | null | undefined;
  configHash?: string | null | undefined;
  mode?: string | null | undefined;
  feedSource?: string | null | undefined;
  venue?: string | null | undefined;
  entrySoftScore?: number | null | undefined;
  /** Tick timestamp → FSM decision on the closing fill (push-source latency). */
  exitTriggerToConfirmMs?: number | null | undefined;
  /** Modelled constant-product impact; already inside feesSol. */
  slippageSol?: number | null | undefined;
  /** dryRunTwin.exitOverrides in force when this twin ran (experiment lane). */
  exitOverridesJson?: string | null | undefined;
  /** PumpSwap tier the entry leg paid (P1.1). */
  feeTierBps?: number | null | undefined;
  mcapSolAtEntry?: number | null | undefined;
  /** Fills produced by the honest simulator (P1.2). */
  simulated?: boolean | null | undefined;
}

/** Raw `dry_run_positions` row as read back (snake_case, plus rowid as `id`). */
export interface DryRunPositionRow {
  id: number;
  mint: string;
  state: string;
  live_status: string;
  live_status_detail: string | null;
  live_status_at_ms: number | null;
  size_sol: number | null;
  entry_price: number | null;
  exit_price: number | null;
  exit_reason: string | null;
  opened_at: string | null;
  closed_at: string | null;
  gross_pnl_sol: number | null;
  fees_sol: number | null;
  net_pnl_sol: number | null;
  pnl_sol: number | null;
  pnl_pct: number | null;
  mfe_pct: number | null;
  mae_pct: number | null;
  time_to_mfe_ms: number | null;
  time_to_mae_ms: number | null;
  hold_ms: number | null;
  fill_count: number;
  samples: number;
  high_volatility: number | null;
  relaxed_risk: number;
  detect_to_open_ms: number | null;
  session_id: number | null;
  config_hash: string | null;
  mode: string | null;
  feed_source: string | null;
  venue: string | null;
  entry_soft_score: number | null;
  exit_trigger_to_confirm_ms: number | null;
  slippage_sol: number | null;
  exit_overrides_json: string | null;
  created_at: string;
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

  /**
   * Persist a pre-graduation launch sighting (S0 observe-only). INSERT OR
   * IGNORE: a mint launches once — cross-feed duplicates and redeliveries
   * collapse onto the first sighting, which is what S1 paper tracking reads.
   */
  recordLaunch(l: FeedLaunch): void {
    this.db
      .prepare(
        `INSERT OR IGNORE INTO launches
           (mint, slot, detected_at_ns, feed_source, name, symbol, uri, creator, signature)
         VALUES (@mint, @slot, @detectedAtNs, @feedSource, @name, @symbol, @uri, @creator, @signature)`,
      )
      .run({
        mint: l.mint,
        slot: l.slot ?? null,
        detectedAtNs: l.receivedAtNs.toString(),
        feedSource: l.feedSource,
        name: l.name ?? null,
        symbol: l.symbol ?? null,
        uri: l.uri ?? null,
        creator: l.creator ?? null,
        signature: l.signature ?? null,
      });
  }

  /**
   * Launch record for a mint (H12 mint-age input). `createdAtMs` is the row's
   * wall-clock insert time (second resolution); `slot` the creation slot when
   * the feed supplied it. detected_at_ns is process hrtime and is NOT
   * comparable across restarts, so it is deliberately not returned.
   */
  launchByMint(mint: string): { slot: number | null; createdAtMs: number | null; creator: string | null } | null {
    const row = this.db
      .prepare(`SELECT slot, created_at, creator FROM launches WHERE mint = ?`)
      .get(mint) as { slot: number | null; created_at: string | null; creator: string | null } | undefined;
    if (!row) return null;
    const raw = row.created_at;
    const parsed = raw ? Date.parse(raw.includes('T') ? raw : `${raw.replace(' ', 'T')}Z`) : NaN;
    return { slot: row.slot, createdAtMs: Number.isFinite(parsed) ? parsed : null, creator: row.creator };
  }

  /** Merge keys into the latest candidate row's features_json (confirm-entry results, P3.2). */
  mergeCandidateFeatures(mint: string, patch: Record<string, unknown>): void {
    this.db
      .prepare(
        `UPDATE candidates SET features_json = json_patch(COALESCE(features_json, '{}'), ?)
         WHERE rowid = (SELECT MAX(rowid) FROM candidates WHERE mint = ?)`,
      )
      .run(JSON.stringify(patch), mint);
  }

  // --- Manipulation-feature caches (P3.3) ---------------------------------

  walletFunder(wallet: string): { funder: string | null; root: string | null } | null {
    const row = this.db.prepare(`SELECT funder, root FROM wallet_funders WHERE wallet = ?`).get(wallet) as
      | { funder: string | null; root: string | null }
      | undefined;
    return row ?? null;
  }

  upsertWalletFunder(wallet: string, funder: string | null, root: string | null): void {
    this.db
      .prepare(
        `INSERT INTO wallet_funders (wallet, funder, root) VALUES (?, ?, ?)
         ON CONFLICT(wallet) DO UPDATE SET funder = excluded.funder, root = excluded.root, resolved_at = datetime('now')`,
      )
      .run(wallet, funder, root);
  }

  /** Launches in the last `days` whose creator belongs to the funding cluster `root`. */
  clusterLaunchCount(root: string, days = 7): { launches: number; wallets: number } {
    const row = this.db
      .prepare(
        `SELECT COUNT(DISTINCT l.mint) AS launches, COUNT(DISTINCT l.creator) AS wallets
         FROM launches l
         WHERE l.created_at >= datetime('now', ?)
           AND (l.creator = ? OR l.creator IN (SELECT wallet FROM wallet_funders WHERE root = ?))`,
      )
      .get(`-${days} days`, root, root) as { launches: number; wallets: number };
    return row;
  }

  /** Launches by one creator wallet in the last `days`. */
  creatorLaunchCount(creator: string, days = 7): number {
    const row = this.db
      .prepare(`SELECT COUNT(*) AS n FROM launches WHERE creator = ? AND created_at >= datetime('now', ?)`)
      .get(creator, `-${days} days`) as { n: number };
    return row.n;
  }

  /** Record a fingerprint and return how many OTHER, earlier mints carry it. */
  recordFingerprint(kind: 'name' | 'image', fingerprint: string, mint: string): number {
    this.db.prepare(`INSERT OR IGNORE INTO metadata_fingerprints (kind, fingerprint, mint) VALUES (?, ?, ?)`).run(kind, fingerprint, mint);
    const row = this.db
      .prepare(`SELECT COUNT(*) AS n FROM metadata_fingerprints WHERE kind = ? AND fingerprint = ? AND mint <> ?`)
      .get(kind, fingerprint, mint) as { n: number };
    return row.n;
  }

  recordSniperObservations(mint: string, wallets: readonly string[]): void {
    const stmt = this.db.prepare(`INSERT OR IGNORE INTO sniper_observations (wallet, mint) VALUES (?, ?)`);
    for (const w of wallets) stmt.run(w, mint);
  }

  /** Distinct mints each wallet was an early buyer of (excluding `excludeMint`), last `days`. */
  sniperCounts(wallets: readonly string[], excludeMint: string, days = 14): Map<string, number> {
    const out = new Map<string, number>();
    if (!wallets.length) return out;
    const stmt = this.db.prepare(
      `SELECT COUNT(DISTINCT mint) AS n FROM sniper_observations
       WHERE wallet = ? AND mint <> ? AND created_at >= datetime('now', ?)`,
    );
    for (const w of new Set(wallets)) out.set(w, (stmt.get(w, excludeMint, `-${days} days`) as { n: number }).n);
    return out;
  }

  insertPathTicks(rows: ReadonlyArray<{ mint: string; arm: string; tMs: number; price: number; quoteReserveSol?: number | null }>): void {
    const stmt = this.db.prepare(`INSERT INTO path_ticks (mint, arm, t_ms, price, quote_reserve) VALUES (?, ?, ?, ?, ?)`);
    for (const r of rows) stmt.run(r.mint, r.arm, Math.round(r.tMs), r.price, r.quoteReserveSol ?? null);
  }

  /** Mints the learning pipeline can label: (mint, arm) with at least `minTicks` path ticks. */
  pathTicks(mint: string, arm: string): Array<{ tMs: number; price: number; quoteReserveSol: number | null }> {
    return (
      this.db
        .prepare(`SELECT t_ms AS tMs, price, quote_reserve AS quoteReserveSol FROM path_ticks WHERE mint = ? AND arm = ? ORDER BY t_ms`)
        .all(mint, arm) as Array<{ tMs: number; price: number; quoteReserveSol: number | null }>
    );
  }

  /** Mints already seen launching (boot dedupe for the launch path). */
  listLaunchMints(): Set<string> {
    const rows = this.db.prepare(`SELECT DISTINCT mint FROM launches`).all() as Array<{ mint: string }>;
    return new Set(rows.map((r) => r.mint));
  }

  /** Launch flow rate probe for S0 volume measurement. */
  countLaunchesSince(createdAt: string): number {
    const row = this.db.prepare(`SELECT COUNT(*) AS n FROM launches WHERE created_at >= ?`).get(createdAt) as {
      n: number;
    };
    return row.n;
  }

  /** Adopt a launch into paper tracking. Re-adopt is a no-op (mint PK). */
  openLaunchTrack(mint: string): void {
    this.db.prepare(`INSERT OR IGNORE INTO launch_tracks (mint) VALUES (?)`).run(mint);
  }

  /** Newest untracked launches first (S1 paper stats cover current flow). */
  listUntrackedLaunches(limit: number): string[] {
    const rows = this.db
      .prepare(
        `SELECT mint FROM launches WHERE mint NOT IN (SELECT mint FROM launch_tracks)
         ORDER BY created_at DESC LIMIT ?`,
      )
      .all(limit) as Array<{ mint: string }>;
    return rows.map((r) => r.mint);
  }

  /** Mints with an open (unclosed) paper track. */
  /**
   * Entry candidates, mature-first (S3): highest sample count, oldest first.
   * The tracker adopts newest-first, so at ~17 launches/min the 10 newest
   * open tracks are always seconds-old early curves that can never pass the
   * late-curve floor. Scanning mature-first keeps the same per-scan RPC
   * budget while actually reaching enrichable late curves.
   */
  listEntryCandidateTracks(limit: number): string[] {
    const rows = this.db
      .prepare(
        `SELECT mint FROM launch_tracks WHERE closed_at IS NULL
         ORDER BY samples DESC, created_at ASC LIMIT ?`,
      )
      .all(limit) as Array<{ mint: string }>;
    return rows.map((r) => r.mint);
  }

  listOpenLaunchTracks(): string[] {
    const rows = this.db
      .prepare(`SELECT mint FROM launch_tracks WHERE closed_at IS NULL ORDER BY created_at DESC`)
      .all() as Array<{ mint: string }>;
    return rows.map((r) => r.mint);
  }

  countUntrackedLaunches(): number {
    const row = this.db
      .prepare(`SELECT COUNT(*) AS n FROM launches WHERE mint NOT IN (SELECT mint FROM launch_tracks)`)
      .get() as { n: number };
    return row.n;
  }

  setLaunchBaseline(mint: string, baselinePrice: number): void {
    this.db
      .prepare(
        `UPDATE launch_tracks SET baseline_price = COALESCE(baseline_price, ?), peak_price = COALESCE(peak_price, ?)
         WHERE mint = ? AND closed_at IS NULL`,
      )
      .run(baselinePrice, baselinePrice, mint);
  }

  updateLaunchTrack(mint: string, baselinePrice: number | null, peakPrice: number | null, samples: number): void {
    this.db
      .prepare(
        `UPDATE launch_tracks SET baseline_price = ?, peak_price = ?, samples = ?
         WHERE mint = ? AND closed_at IS NULL`,
      )
      .run(baselinePrice, peakPrice, samples, mint);
  }

  closeLaunchTrack(o: {
    mint: string;
    baselinePrice: number | null;
    peakPrice: number | null;
    peakMfePct: number | null;
    graduated: boolean;
    trackedMs: number;
    samples: number;
    sessionId: number | null;
    configHash: string | null;
  }): void {
    this.db
      .prepare(
        `UPDATE launch_tracks SET baseline_price = ?, peak_price = ?, peak_mfe_pct = ?,
           graduated = ?, graduated_at = CASE WHEN ? THEN datetime('now') ELSE graduated_at END,
           time_to_graduation_ms = CASE WHEN ? THEN ? ELSE time_to_graduation_ms END,
           tracked_ms = ?, samples = ?, session_id = ?, config_hash = ?, closed_at = datetime('now')
         WHERE mint = ? AND closed_at IS NULL`,
      )
      .run(
        o.baselinePrice,
        o.peakPrice,
        o.peakMfePct,
        o.graduated ? 1 : 0,
        o.graduated ? 1 : 0,
        o.graduated ? 1 : 0,
        o.trackedMs,
        o.trackedMs,
        o.samples,
        o.sessionId,
        o.configHash,
        o.mint,
      );
  }

  /** True once the mint has a graduation row (S1 reconcile: paper vs real). */
  isGraduated(mint: string): boolean {
    return this.db.prepare(`SELECT 1 FROM graduations WHERE mint = ?`).get(mint) !== undefined;
  }

  /**
   * Migrated PumpSwap pool for a graduated mint (S4 venue switch). Null when
   * the graduation row is missing or carries no pool address — the caller
   * keeps the position parked, never sells blind.
   */
  getGraduationPool(mint: string): string | null {
    const row = this.db
      .prepare(`SELECT pool_address AS pool FROM graduations WHERE mint = ? ORDER BY rowid DESC LIMIT 1`)
      .get(mint) as { pool: string | null } | undefined;
    const pool = row?.pool ?? null;
    return pool !== null && pool !== '' ? pool : null;
  }

  // -- curve_positions (pre-graduation live lane, S3b) ---------------------

  recordCurvePosition(p: {
    mint: string;
    state: string;
    sizeSol: number;
    entryPrice?: number | null;
    entryBaseAmount?: string | null;
    entryTx?: string | null;
    relaxedRisk?: boolean;
    relaxedReasonsJson?: string | null;
    executionJson?: string | null;
    isToken2022?: boolean;
    sessionId?: number | null;
    configHash?: string | null;
    openedAt?: string | null;
  }): number {
    const row = this.db
      .prepare(
        `INSERT INTO curve_positions
           (mint, state, size_sol, entry_price, entry_base_amount, entry_tx,
            relaxed_risk, relaxed_reasons_json, execution_json, is_token_2022,
            session_id, config_hash, opened_at)
         VALUES (@mint, @state, @sizeSol, @entryPrice, @entryBaseAmount, @entryTx,
            @relaxedRisk, @relaxedReasonsJson, @executionJson, @isToken2022,
            @sessionId, @configHash, @openedAt)
         RETURNING rowid`,
      )
      .get({
        mint: p.mint,
        state: p.state,
        sizeSol: p.sizeSol,
        entryPrice: p.entryPrice ?? null,
        entryBaseAmount: p.entryBaseAmount ?? null,
        entryTx: p.entryTx ?? null,
        relaxedRisk: p.relaxedRisk ? 1 : 0,
        relaxedReasonsJson: p.relaxedReasonsJson ?? null,
        executionJson: p.executionJson ?? null,
        isToken2022: p.isToken2022 ? 1 : 0,
        sessionId: p.sessionId ?? null,
        configHash: p.configHash ?? null,
        openedAt: p.openedAt ?? null,
      }) as { rowid: number };
    return row.rowid;
  }

  updateCurvePositionState(rowid: number, state: string, patch: Record<string, unknown> = {}): void {
    const sets = ['state = @state', ...Object.keys(patch).map((k) => `${k} = @${k}`)];
    this.db.prepare(`UPDATE curve_positions SET ${sets.join(', ')} WHERE rowid = @rowid`).run({ ...patch, state, rowid });
  }

  listCurvePositionsByState(state: string): Array<Record<string, unknown>> {
    return this.db.prepare(`SELECT rowid, * FROM curve_positions WHERE state = ? ORDER BY rowid`).all(state) as Array<
      Record<string, unknown>
    >;
  }

  /** Realized curve PnL since a UTC threshold (dedicated sublimit input). */
  curveRealizedSince(createdAt: string): number {
    const row = this.db
      .prepare(`SELECT COALESCE(SUM(net_pnl_sol), 0) AS s FROM curve_positions WHERE state = 'CLOSED' AND closed_at >= ?`)
      .get(createdAt) as { s: number };
    return row.s;
  }

  /** Consecutive curve losses (own halt input — mirrors the global breaker). */
  curveConsecutiveLosses(limit: number): number {
    const rows = this.db
      .prepare(`SELECT net_pnl_sol AS n FROM curve_positions WHERE state = 'CLOSED' ORDER BY closed_at DESC LIMIT ?`)
      .all(limit) as Array<{ n: number | null }>;
    let streak = 0;
    for (const r of rows) {
      if (r.n !== null && r.n < 0) streak++;
      else break;
    }
    return streak;
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
            relaxed_risk, relaxed_reasons_json, sellability_reason, sellability_tx_bytes,
            sellability_used_lookup_table, primary_veto_code,
            session_id, config_hash, size_multiplier, early_flow_net_sol, early_flow_rate, pool_sol_at_entry, buy_impact_pct,
            top10_share, max_holder_share, creator_share, rugcheck_score, has_socials, score_components_json,
            unknowns_json, enrichment_ms, momentum_window_ms,
            sellability_status, pool_move_pct, mint_age_ms, creator, mcap_sol_at_entry, fee_tier_bps, population_ok,
            features_json, model_version, model_prob)
         VALUES (@mint, @enrichment, @hardChecks, @softScore, @verdict, @vetoReasons, @highVol,
            @relaxedRisk, @relaxedReasonsJson, @sellabilityReason, @sellabilityTxBytes,
            @sellabilityUsedLookupTable, @primaryVeto,
            @sessionId, @configHash, @sizeMultiplier, @earlyFlowNetSol, @earlyFlowRate, @poolSolAtEntry, @buyImpactPct,
            @top10Share, @maxHolderShare, @creatorShare, @rugcheckScore, @hasSocials, @scoreComponentsJson,
            @unknownsJson, @enrichmentMs, @momentumWindowMs,
            @sellabilityStatus, @poolMovePct, @mintAgeMs, @creator, @mcapSolAtEntry, @feeTierBps, @populationOk,
            @featuresJson, @modelVersion, @modelProb)`,
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
        sellabilityTxBytes: features.sellabilityTxBytes ?? null,
        sellabilityUsedLookupTable:
          features.sellabilityUsedLookupTable === null || features.sellabilityUsedLookupTable === undefined
            ? null
            : features.sellabilityUsedLookupTable ? 1 : 0,
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
        sellabilityStatus: features.sellabilityStatus ?? v.hardChecks.find((c) => c.id === 'H4')?.status ?? null,
        poolMovePct: features.poolMovePct ?? null,
        mintAgeMs: features.mintAgeMs ?? null,
        creator: features.creator ?? null,
        mcapSolAtEntry: features.mcapSolAtEntry ?? null,
        feeTierBps: features.feeTierBps ?? null,
        populationOk: boolInt(features.populationOk),
        featuresJson: features.featuresJson ?? null,
        modelVersion: features.modelVersion ?? null,
        modelProb: features.modelProb ?? null,
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
            score_components_json, unknowns_json, enrichment_ms, slippage_sol,
            ticks_observed, suspect_ticks, first_tick_ms, entry_move_from_detect_pct,
            sellability_status, sellability_reason, pool_move_pct, mint_age_ms, creator, mcap_sol_at_entry,
            fee_tier_bps, population_ok, simulated, features_json, model_version, model_prob)
         VALUES (@mint, @entryTx, @entryPrice, @exitPrice, @sizeSol, @state, @exitReason, @exitTx, @pnlSol, @pnlPct, @openedAt, @closedAt,
                 @rawBaseAmount, @pricingJson, @executionJson, @exitIntentJson, @relaxedRisk, @relaxedReasonsJson,
                 @exitTriggerToConfirmMs, @momentumWindowMs,
                 @grossPnlSol, @feesSol, @netPnlSol, @entrySoftScore, @highVolatility, @mfePct, @maePct, @holdMs,
                 @feedSource, @venue, @mode, @sessionId, @configHash, @timeToMfeMs, @timeToMaeMs, @pathMarksJson,
                 @leftOnTablePct, @detectToOpenMs, @sizeMultiplier, @earlyFlowNetSol, @earlyFlowRate, @poolSolAtEntry,
                 @buyImpactPct, @top10Share, @maxHolderShare, @creatorShare, @rugcheckScore, @hasSocials,
                 @scoreComponentsJson, @unknownsJson, @enrichmentMs, @slippageSol,
                 @ticksObserved, @suspectTicks, @firstTickMs, @entryMoveFromDetectPct,
                 @sellabilityStatus, @sellabilityReason, @poolMovePct, @mintAgeMs, @creator, @mcapSolAtEntry,
                 @feeTierBps, @populationOk, @simulated, @featuresJson, @modelVersion, @modelProb)`,
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
        slippageSol: txns.slippageSol ?? null,
        ticksObserved: txns.ticksObserved ?? null,
        suspectTicks: txns.suspectTicks ?? null,
        firstTickMs: txns.firstTickMs ?? null,
        entryMoveFromDetectPct: txns.entryMoveFromDetectPct ?? null,
        sellabilityStatus: txns.sellabilityStatus ?? null,
        sellabilityReason: txns.sellabilityReason ?? null,
        poolMovePct: txns.poolMovePct ?? null,
        mintAgeMs: txns.mintAgeMs ?? null,
        creator: txns.creator ?? null,
        mcapSolAtEntry: txns.mcapSolAtEntry ?? null,
        feeTierBps: txns.feeTierBps ?? null,
        populationOk: boolInt(txns.populationOk),
        simulated: boolInt(txns.simulated),
        featuresJson: txns.featuresJson ?? null,
        modelVersion: txns.modelVersion ?? null,
        modelProb: txns.modelProb ?? null,
      });
  }

  startRunSession(input: {
    mode: string;
    configHash: string;
    configJson: string;
    gitCommit?: string | null;
    hypothesis?: string | null;
  }): number {
    const info = this.db
      .prepare(
        `INSERT INTO run_sessions (mode, config_hash, config_json, git_commit, hypothesis)
         VALUES (@mode, @configHash, @configJson, @gitCommit, @hypothesis)`,
      )
      .run({
        mode: input.mode,
        configHash: input.configHash,
        configJson: input.configJson,
        gitCommit: input.gitCommit ?? null,
        hypothesis: input.hypothesis ?? null,
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
                has_socials, score_components_json, unknowns_json, enrichment_ms, momentum_window_ms,
                sellability_status, pool_move_pct, mint_age_ms, creator, mcap_sol_at_entry, fee_tier_bps, population_ok,
                features_json, model_version, model_prob
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
          sellability_status: string | null;
          pool_move_pct: number | null;
          mint_age_ms: number | null;
          creator: string | null;
          mcap_sol_at_entry: number | null;
          fee_tier_bps: number | null;
          population_ok: number | null;
          features_json: string | null;
          model_version: string | null;
          model_prob: number | null;
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
      sellabilityStatus: row.sellability_status,
      poolMovePct: row.pool_move_pct,
      mintAgeMs: row.mint_age_ms,
      creator: row.creator,
      mcapSolAtEntry: row.mcap_sol_at_entry,
      feeTierBps: row.fee_tier_bps,
      populationOk: row.population_ok === null ? null : row.population_ok === 1,
      featuresJson: row.features_json,
      modelVersion: row.model_version,
      modelProb: row.model_prob,
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
    kind: LatencyKind;
    /** Milliseconds — or a SLOT COUNT for the `*_slots` kinds. */
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

  /**
   * Recent latency samples of one kind, newest first — the honest simulator's
   * empirical pool (P1.2). Only live confirms are ever written here.
   */
  recentLatencySamples(kind: LatencyKind, sinceDays = 14, limit = 5_000): number[] {
    const rows = this.db
      .prepare(
        `SELECT latency_ms AS v FROM latency_samples
         WHERE kind = ? AND created_at >= datetime('now', ?)
         ORDER BY id DESC LIMIT ?`,
      )
      .all(kind, `-${sinceDays} days`, limit) as Array<{ v: number }>;
    return rows.map((r) => r.v);
  }

  /** Persist a counterfactual dry-run outcome for a candidate we did not trade. */
  recordShadowOutcome(o: {
    mint: string;
    verdict: 'veto' | 'accept_not_entered' | 'confirm_arm';
    /** 'veto' -> shadow_outcomes; any other arm -> confirm_outcomes (P3.2). */
    arm?: string;
    primaryVetoCode: string | null;
    /** Full set of red-flag / veto codes when available. */
    vetoCodes?: string[] | null;
    baselinePrice: number;
    peakPrice: number;
    troughPrice: number;
    peakMfePct: number;
    maxMaePct: number;
    hit25: boolean;
    hit50: boolean;
    samples: number;
    trackedMs: number;
    /** Simulated size used for fee-adjusted PnL (SOL). */
    sizeSol?: number | null;
    grossPnlSol?: number | null;
    feesSol?: number | null;
    netPnlSol?: number | null;
    pnlPct?: number | null;
    exitReason?: string | null;
    holdMs?: number | null;
    sessionId?: number | null;
    configHash?: string | null;
    outcomeVersion?: 'exit_fsm_v1' | 'exit_fsm_v2' | 'exit_fsm_v2_sim' | null;
  }): void {
    this.db
      .prepare(
        `INSERT INTO ${o.arm && o.arm !== 'veto' ? 'confirm_outcomes' : 'shadow_outcomes'}
           (mint, verdict, primary_veto_code, veto_codes_json, baseline_price, peak_price, trough_price,
            peak_mfe_pct, max_mae_pct, hit_25, hit_50, samples, tracked_ms,
            size_sol, gross_pnl_sol, fees_sol, net_pnl_sol, pnl_pct, exit_reason, hold_ms,
            session_id, config_hash, outcome_version, arm)
         VALUES (@mint, @verdict, @primaryVetoCode, @vetoCodesJson, @baselinePrice, @peakPrice, @troughPrice,
            @peakMfePct, @maxMaePct, @hit25, @hit50, @samples, @trackedMs,
            @sizeSol, @grossPnlSol, @feesSol, @netPnlSol, @pnlPct, @exitReason, @holdMs,
            @sessionId, @configHash, @outcomeVersion, @arm)`,
      )
      .run({
        mint: o.mint,
        verdict: o.verdict,
        primaryVetoCode: o.primaryVetoCode,
        vetoCodesJson: o.vetoCodes?.length ? JSON.stringify(o.vetoCodes) : null,
        baselinePrice: o.baselinePrice,
        peakPrice: o.peakPrice,
        troughPrice: o.troughPrice,
        peakMfePct: o.peakMfePct,
        maxMaePct: o.maxMaePct,
        hit25: o.hit25 ? 1 : 0,
        hit50: o.hit50 ? 1 : 0,
        samples: o.samples,
        trackedMs: o.trackedMs,
        sizeSol: o.sizeSol ?? null,
        grossPnlSol: o.grossPnlSol ?? null,
        feesSol: o.feesSol ?? null,
        netPnlSol: o.netPnlSol ?? null,
        pnlPct: o.pnlPct ?? null,
        exitReason: o.exitReason ?? null,
        holdMs: o.holdMs ?? null,
        sessionId: o.sessionId ?? null,
        configHash: o.configHash ?? null,
        outcomeVersion: o.outcomeVersion ?? (o.netPnlSol == null ? null : 'exit_fsm_v1'),
        arm: o.arm ?? 'veto',
      });
  }

  recordShadowCoverage(
    kind: 'eligible' | 'started' | 'skipped_missing_pricing' | 'dropped_capacity',
    mint?: string,
  ): void {
    this.db
      .prepare(`INSERT INTO shadow_coverage_events (kind, mint) VALUES (?, ?)`)
      .run(kind, mint ?? null);
  }

  /**
   * Persist a dry-run twin row. Append-only, matching `upsertPosition` — one row
   * per FSM transition, "current" is MAX(rowid) per mint.
   *
   * Writes to `dry_run_positions`, NEVER to `positions`. That separation is the
   * safety property of this whole feature: `positions` is read unfiltered by the
   * kill switch, daily-loss limit, consecutive-loss halt and crash recovery.
   */
  upsertDryRunPosition(row: DryRunPositionInput): void {
    this.db
      .prepare(
        `INSERT INTO dry_run_positions
           (mint, state, live_status, live_status_detail, live_status_at_ms,
            size_sol, entry_price, exit_price, exit_reason, opened_at, closed_at,
            gross_pnl_sol, fees_sol, net_pnl_sol, pnl_sol, pnl_pct,
            mfe_pct, mae_pct, time_to_mfe_ms, time_to_mae_ms, hold_ms,
            fill_count, samples, high_volatility, relaxed_risk, detect_to_open_ms,
            session_id, config_hash, mode,
            feed_source, venue, entry_soft_score, exit_trigger_to_confirm_ms, slippage_sol, exit_overrides_json,
            fee_tier_bps, mcap_sol_at_entry, simulated)
         VALUES (@mint, @state, @liveStatus, @liveStatusDetail, @liveStatusAtMs,
            @sizeSol, @entryPrice, @exitPrice, @exitReason, @openedAt, @closedAt,
            @grossPnlSol, @feesSol, @netPnlSol, @pnlSol, @pnlPct,
            @mfePct, @maePct, @timeToMfeMs, @timeToMaeMs, @holdMs,
            @fillCount, @samples, @highVolatility, @relaxedRisk, @detectToOpenMs,
            @sessionId, @configHash, @mode,
            @feedSource, @venue, @entrySoftScore, @exitTriggerToConfirmMs, @slippageSol, @exitOverridesJson,
            @feeTierBps, @mcapSolAtEntry, @simulated)`,
      )
      .run({
        mint: row.mint,
        state: row.state,
        liveStatus: row.liveStatus,
        liveStatusDetail: row.liveStatusDetail ?? null,
        liveStatusAtMs: row.liveStatusAtMs ?? null,
        sizeSol: row.sizeSol ?? null,
        entryPrice: row.entryPrice ?? null,
        exitPrice: row.exitPrice ?? null,
        exitReason: row.exitReason ?? null,
        openedAt: row.openedAt ? new Date(row.openedAt).toISOString() : null,
        closedAt: row.closedAt ? new Date(row.closedAt).toISOString() : null,
        grossPnlSol: row.grossPnlSol ?? null,
        feesSol: row.feesSol ?? null,
        netPnlSol: row.netPnlSol ?? null,
        // pnl_sol mirrors net so the row is shape-compatible with `positions`.
        pnlSol: row.netPnlSol ?? null,
        pnlPct: row.pnlPct ?? null,
        mfePct: row.mfePct ?? null,
        maePct: row.maePct ?? null,
        timeToMfeMs: row.timeToMfeMs ?? null,
        timeToMaeMs: row.timeToMaeMs ?? null,
        holdMs: row.holdMs ?? null,
        fillCount: row.fillCount ?? 0,
        samples: row.samples ?? 0,
        highVolatility: row.highVolatility === undefined ? null : row.highVolatility ? 1 : 0,
        relaxedRisk: row.relaxedRisk ? 1 : 0,
        detectToOpenMs: row.detectToOpenMs ?? null,
        sessionId: row.sessionId ?? null,
        configHash: row.configHash ?? null,
        mode: row.mode ?? null,
        feedSource: row.feedSource ?? null,
        venue: row.venue ?? null,
        entrySoftScore: row.entrySoftScore ?? null,
        exitTriggerToConfirmMs: row.exitTriggerToConfirmMs ?? null,
        slippageSol: row.slippageSol ?? null,
        exitOverridesJson: row.exitOverridesJson ?? null,
        feeTierBps: row.feeTierBps ?? null,
        mcapSolAtEntry: row.mcapSolAtEntry ?? null,
        simulated: boolInt(row.simulated),
      });
  }

  recordDryRunCoverage(kind: DryRunCoverageKind, mint?: string, detail?: string): void {
    this.db
      .prepare(`INSERT INTO dry_run_coverage_events (kind, mint, detail) VALUES (?, ?, ?)`)
      .run(kind, mint ?? null, detail ?? null);
  }

  /** Latest twin row per mint (the append-only "current state" read). */
  latestDryRunPositions(): DryRunPositionRow[] {
    return this.db
      .prepare(
        `SELECT rowid AS id, * FROM dry_run_positions
         WHERE rowid IN (SELECT MAX(rowid) FROM dry_run_positions GROUP BY mint)`,
      )
      .all() as unknown as DryRunPositionRow[];
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

  /**
   * Operator day-risk reset (RESET_DAY sentinel). Durable one-shot marker:
   * rehydration counts daily PnL and the consecutive-loss streak only from
   * this timestamp, so an explicit operator reset survives restarts but never
   * silently washes out a tripped breaker. Always stored as ISO UTC.
   */
  recordRiskDayReset(reason: string, atMs = Date.now()): string {
    const at = new Date(atMs).toISOString();
    this.db.prepare(`INSERT INTO risk_day_resets (at, reason) VALUES (?, ?)`).run(at, reason);
    return at;
  }

  /** Latest operator day-risk reset timestamp (ISO UTC), or null if none. */
  lastRiskDayResetAt(): string | null {
    const row = this.db
      .prepare(`SELECT at FROM risk_day_resets ORDER BY rowid DESC LIMIT 1`)
      .get() as { at: string } | undefined;
    return row?.at ?? null;
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

  /**
   * Every mint that has ever fired a `graduation` event, across all sessions.
   * A genuine pump.fun bonding curve completes at most once, so any further
   * "graduation" for an already-seen mint is definitionally spurious (a stale
   * detector match, not a fresh migration) — the detector uses this to
   * permanently veto reprocessing a mint, independent of the in-memory
   * cross-feed dedupe TTL.
   */
  listGraduatedMints(): Set<string> {
    const rows = this.db.prepare(`SELECT DISTINCT mint FROM graduations`).all() as Array<{ mint: string }>;
    return new Set(rows.map((r) => r.mint));
  }
}

function jsonReplacer(_key: string, value: unknown): unknown {
  if (typeof value === 'bigint') return value.toString();
  return value;
}

function boolInt(v: boolean | null | undefined): number | null {
  return v === null || v === undefined ? null : v ? 1 : 0;
}
