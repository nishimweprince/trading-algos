import type { DB } from '../persistence/db.ts';
import type { Config } from '../config/schema.ts';
import { positionsModeFilterSql } from '../persistence/modeFilter.ts';
import type { OperatorEventLevel } from '../persistence/repositories.ts';
import type { RiskSnapshot } from '../risk/manager.ts';
import { paperRoundTripPct, type PoolMark } from '../racer/economics.ts';
import { EXIT_RULES, type ExitRule, type ExitResult } from '../racer/pathExit.ts';
import {
  computePerformanceStats,
  latencyPercentiles,
  loadClosedPnls,
  rangeToModifier,
  type PerformanceStats,
} from './analytics.ts';

type Row = Record<string, unknown>;

const ACTIVE_STATES = ['PENDING_ENTRY', 'OPEN', 'EXITING'];

/**
 * Which leg of the dual-track run a read is scoped to.
 *
 *   live  — real capital: the `positions` table
 *   dry   — the ideal paper twin: the `dry_run_positions` table
 *   delta — live vs dry, served only by getExecutionDragComparison
 *
 * `delta` is deliberately NOT accepted by the summary / positions readers. Those
 * return wallet-shaped types, and a delta rendered in a wallet shape invites a
 * drag number being read as a balance.
 */
export type Track = 'live' | 'dry' | 'delta';
export type DataTrack = 'live' | 'dry';

type PositionsTable = 'positions' | 'dry_run_positions';

/** Literal table name for a track — never interpolated from user input. */
function trackTable(track: DataTrack | undefined): PositionsTable {
  return track === 'dry' ? 'dry_run_positions' : 'positions';
}

/**
 * Where a tab's rows come from, given the mode the bot runs in.
 *
 *   dry-run bot: dry tab → its own simulated trades (`positions`, mode
 *                'dry-run'); live tab → real-capital rows only (mode 'live'),
 *                so simulated numbers never sit under "Live".
 *   live / paper bot: live tab → the bot's own rows (`positions`, its mode);
 *                dry tab → the paper twin (`dry_run_positions`).
 *
 * `botMode` undefined keeps the pre-2026-10 behaviour (no mode filter).
 */
export interface TrackSource {
  table: PositionsTable;
  /** Mode filter for `positions` rows; undefined = none. */
  mode: Config['mode'] | undefined;
  /** The paper twin: no marks of its own, live-only blotter columns blank. */
  isTwin: boolean;
}

export function trackSource(track: DataTrack | undefined, botMode: Config['mode'] | undefined): TrackSource {
  if (botMode === 'dry-run') {
    return track === 'dry'
      ? { table: 'positions', mode: 'dry-run', isTwin: false }
      : { table: 'positions', mode: 'live', isTwin: false };
  }
  if (track === 'dry') return { table: 'dry_run_positions', mode: undefined, isTwin: true };
  return { table: 'positions', mode: botMode, isTwin: false };
}

/** Mode filter (leading " AND …") for a source; '' for the twin table. */
function sourceFilter(src: TrackSource, alias = ''): string {
  return src.table === 'positions' ? positionsModeFilterSql(src.mode, alias) : '';
}

export interface DashboardSummary {
  mode: Config['mode'];
  pnl: {
    realizedSol: number;
    realized24hSol: number;
    realized7dSol: number;
    winRatePct: number;
    closedCount: number;
    wins: number;
    losses: number;
    expectancySol: number;
    profitFactor: number;
    maxDrawdownSol: number;
    currentDrawdownSol: number;
    avgWinSol: number;
    avgLossSol: number;
    feesSol: number;
    unrealizedSol: number;
  };
  positions: {
    openCount: number;
    openExposureSol: number;
    maxConcurrent: number;
    pendingCount: number;
    exitingCount: number;
    failedCount: number;
  };
  flow: {
    graduations: number;
    accepted: number;
    vetoed: number;
    highVolatility: number;
  };
  latency: {
    detection: { count: number; p50: number; p95: number; max: number };
    exitConfirm: { count: number; p50: number; p95: number; max: number };
    /** Chain-relative (values are SLOT counts, not ms). */
    detectionSlots: { count: number; p50: number; p95: number; max: number };
    entryLandSlots: { count: number; p50: number; p95: number; max: number };
    exitLandSlots: { count: number; p50: number; p95: number; max: number };
  };
  system: {
    latestBreaker: { type: string; detail: string | null; tripped: boolean; at: string } | null;
    latestEventAt: string | null;
    lastGraduationAt: string | null;
  };
}

export interface DashboardPosition {
  id: number;
  mint: string;
  state: string;
  entryTx: string | null;
  entryPrice: number | null;
  exitPrice: number | null;
  sizeSol: number;
  exitReason: string | null;
  exitTx: string | null;
  pnlSol: number | null;
  pnlPct: number | null;
  grossPnlSol: number | null;
  feesSol: number | null;
  netPnlSol: number | null;
  entrySoftScore: number | null;
  highVolatility: boolean | null;
  mfePct: number | null;
  maePct: number | null;
  holdMs: number | null;
  exitTriggerToConfirmMs: number | null;
  feedSource: string | null;
  venue: string | null;
  mode: string | null;
  relaxedRisk: boolean;
  relaxedReasons: string[];
  openedAt: string | null;
  closedAt: string | null;
  createdAt: string;
  unrealizedSol: number | null;
}

export interface PnlPoint {
  time: string;
  mint: string;
  pnlSol: number;
  cumulativePnlSol: number;
}

export interface DashboardCandidate {
  id: number;
  mint: string;
  verdict: string | null;
  softScore: number | null;
  vetoReasons: string[];
  highVolatility: boolean;
  relaxedRisk: boolean;
  relaxedReasons: string[];
  createdAt: string;
  hardChecks?: Array<{ id: string; label?: string; status: string; detail?: string; reason?: string }>;
}

export interface DashboardEvent {
  id: number;
  category: string;
  level: OperatorEventLevel;
  message: string;
  entityMint: string | null;
  payload: unknown;
  createdAt: string;
}

export interface ExitReasonRow {
  reason: string;
  count: number;
  pnlSol: number;
  winRatePct: number;
}

export interface FunnelStats {
  graduations: number;
  accepted: number;
  vetoed: number;
  entered: number;
  closed: number;
  failed: number;
  acceptRatePct: number;
  entryRatePct: number;
}

export interface BreakerRow {
  type: string;
  detail: string | null;
  tripped: boolean;
  at: string;
}

export function getDashboardSummary(
  db: DB,
  config: Config,
  opts: { track?: DataTrack } = {},
): DashboardSummary {
  const src = trackSource(opts.track, config.mode);
  const table = src.table;
  const isDry = src.isTwin;
  // Live tab = real capital only; dry tab = this bot's simulated trades (or the twin).
  const mf = sourceFilter(src);
  const closed = one<{ total: number; count: number; wins: number; losses: number; fees: number }>(
    db,
    `SELECT
       COALESCE(SUM(COALESCE(net_pnl_sol, pnl_sol)), 0) AS total,
       COUNT(*) AS count,
       COALESCE(SUM(CASE WHEN COALESCE(net_pnl_sol, pnl_sol) > 0 THEN 1 ELSE 0 END), 0) AS wins,
       COALESCE(SUM(CASE WHEN COALESCE(net_pnl_sol, pnl_sol) <= 0 THEN 1 ELSE 0 END), 0) AS losses,
       COALESCE(SUM(COALESCE(fees_sol, 0)), 0) AS fees
     FROM ${table}
     WHERE state = 'CLOSED'${mf}`,
  );
  const recent = one<{ pnl24h: number; pnl7d: number }>(
    db,
    `SELECT
       COALESCE(SUM(CASE WHEN closed_at IS NOT NULL AND julianday(closed_at) >= julianday('now', '-1 day') THEN COALESCE(net_pnl_sol, pnl_sol) ELSE 0 END), 0) AS pnl24h,
       COALESCE(SUM(CASE WHEN closed_at IS NOT NULL AND julianday(closed_at) >= julianday('now', '-7 days') THEN COALESCE(net_pnl_sol, pnl_sol) ELSE 0 END), 0) AS pnl7d
     FROM ${table}
     WHERE state = 'CLOSED'${mf}`,
  );
  const active = one<{ count: number; exposure: number; pending: number; exiting: number }>(
    db,
    `WITH latest AS (${latestPositionsSql(table)})
     SELECT
       COUNT(*) AS count,
       COALESCE(SUM(size_sol), 0) AS exposure,
       COALESCE(SUM(CASE WHEN state = 'PENDING_ENTRY' THEN 1 ELSE 0 END), 0) AS pending,
       COALESCE(SUM(CASE WHEN state = 'EXITING' THEN 1 ELSE 0 END), 0) AS exiting
     FROM latest
     WHERE state IN (${ACTIVE_STATES.map((s) => `'${s}'`).join(',')})${mf}`,
  );
  const failed = one<{ n: number }>(
    db,
    `WITH latest AS (${latestPositionsSql(table)})
     SELECT COUNT(*) AS n FROM latest WHERE state = 'FAILED'${mf}`,
  );
  const flow = one<{ graduations: number; accepted: number; vetoed: number; highVol: number }>(
    db,
    `SELECT
       (SELECT COUNT(*) FROM graduations) AS graduations,
       (SELECT COUNT(*) FROM candidates WHERE verdict = 'accept') AS accepted,
       (SELECT COUNT(*) FROM candidates WHERE verdict = 'veto') AS vetoed,
       (SELECT COUNT(*) FROM candidates WHERE high_volatility = 1) AS highVol`,
  );
  const latestBreaker = db
    .prepare(
      `SELECT type, detail, tripped, at
       FROM breaker_events
       ORDER BY rowid DESC
       LIMIT 1`,
    )
    .get() as { type: string; detail: string | null; tripped: number; at: string } | undefined;
  const latestEvent = one<{ at: string | null }>(
    db,
    `SELECT MAX(created_at) AS at FROM operator_events`,
  );
  const lastGrad = one<{ at: string | null }>(
    db,
    `SELECT MAX(created_at) AS at FROM graduations`,
  );

  const { pnls, fees } = loadClosedPnls(db, undefined, table, src.mode);
  const perf = computePerformanceStats(pnls, fees);
  // Mark-to-market comes from price_ticks, which only the bot's own poller writes.
  // A dry twin has no mark of its own; reporting the bot's would be fiction.
  const unrealizedSol = isDry ? 0 : computeUnrealizedPnl(db, sourceFilter(src, 'l'));

  return {
    mode: config.mode,
    pnl: {
      realizedSol: closed.total,
      realized24hSol: recent.pnl24h,
      realized7dSol: recent.pnl7d,
      winRatePct: closed.count > 0 ? (closed.wins / closed.count) * 100 : 0,
      closedCount: closed.count,
      wins: closed.wins,
      losses: closed.losses,
      expectancySol: perf.expectancySol,
      profitFactor: perf.profitFactor,
      maxDrawdownSol: perf.maxDrawdownSol,
      currentDrawdownSol: perf.currentDrawdownSol,
      avgWinSol: perf.avgWinSol,
      avgLossSol: perf.avgLossSol,
      feesSol: closed.fees,
      unrealizedSol,
    },
    positions: {
      openCount: active.count,
      openExposureSol: active.exposure,
      maxConcurrent: config.risk.maxConcurrentPositions,
      pendingCount: active.pending,
      exitingCount: active.exiting,
      failedCount: failed.n,
    },
    flow: {
      graduations: flow.graduations,
      accepted: flow.accepted,
      vetoed: flow.vetoed,
      highVolatility: flow.highVol,
    },
    latency: {
      detection: latencyPercentiles(db, 'detection', '-1 day'),
      exitConfirm: latencyPercentiles(db, 'exit_confirm', '-1 day'),
      detectionSlots: latencyPercentiles(db, 'detection_slots', '-1 day'),
      entryLandSlots: latencyPercentiles(db, 'entry_land_slots', '-1 day'),
      exitLandSlots: latencyPercentiles(db, 'exit_land_slots', '-1 day'),
    },
    system: {
      latestBreaker: latestBreaker
        ? {
            type: latestBreaker.type,
            detail: latestBreaker.detail,
            tripped: latestBreaker.tripped === 1,
            at: latestBreaker.at,
          }
        : null,
      latestEventAt: latestEvent.at,
      lastGraduationAt: lastGrad.at,
    },
  };
}

export function getPerformanceAnalytics(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all'; track?: DataTrack; mode?: Config['mode'] } = {},
): PerformanceStats & { exitReasons: ExitReasonRow[] } {
  const mod = rangeToModifier(opts.range);
  const src = trackSource(opts.track, opts.mode);
  const { pnls, fees } = loadClosedPnls(db, mod, src.table, src.mode);
  const stats = computePerformanceStats(pnls, fees);
  return { ...stats, exitReasons: listExitReasonBreakdown(db, mod, src.table, src.mode) };
}

export function getFunnelAnalytics(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): FunnelStats {
  const mod = rangeToModifier(opts.range);
  const whereTime = mod ? `AND julianday(created_at) >= julianday('now', '${mod}')` : '';
  const whereClosed = mod
    ? `AND closed_at IS NOT NULL AND julianday(closed_at) >= julianday('now', '${mod}')`
    : '';

  const row = one<{
    graduations: number;
    accepted: number;
    vetoed: number;
    entered: number;
    closed: number;
    failed: number;
  }>(
    db,
    `SELECT
       (SELECT COUNT(*) FROM graduations WHERE 1=1 ${whereTime}) AS graduations,
       (SELECT COUNT(*) FROM candidates WHERE verdict = 'accept' ${whereTime}) AS accepted,
       (SELECT COUNT(*) FROM candidates WHERE verdict = 'veto' ${whereTime}) AS vetoed,
       -- positions is append-only (one row per FSM transition), so COUNT(*) would
       -- over-count a mint once per state change. Dedupe by mint to count entities.
       (SELECT COUNT(DISTINCT mint) FROM positions WHERE state IN ('PENDING_ENTRY','OPEN','EXITING','CLOSED','FAILED') ${whereTime}) AS entered,
       (SELECT COUNT(DISTINCT mint) FROM positions WHERE state = 'CLOSED' ${whereClosed}) AS closed,
       (SELECT COUNT(DISTINCT mint) FROM positions WHERE state = 'FAILED' ${whereTime}) AS failed`,
  );

  const screened = row.accepted + row.vetoed;
  return {
    ...row,
    acceptRatePct: screened > 0 ? (row.accepted / screened) * 100 : 0,
    entryRatePct: row.accepted > 0 ? (row.entered / row.accepted) * 100 : 0,
  };
}

export function listExitReasonBreakdown(
  db: DB,
  sinceModifier?: string,
  table: PositionsTable = 'positions',
  mode?: Config['mode'],
): ExitReasonRow[] {
  const mf = table === 'positions' ? positionsModeFilterSql(mode) : '';
  const sql = sinceModifier
    ? `SELECT COALESCE(exit_reason, 'UNKNOWN') AS reason,
              COUNT(*) AS count,
              COALESCE(SUM(COALESCE(net_pnl_sol, pnl_sol)), 0) AS pnlSol,
              COALESCE(SUM(CASE WHEN COALESCE(net_pnl_sol, pnl_sol) > 0 THEN 1 ELSE 0 END), 0) AS wins
       FROM ${table}
       WHERE state = 'CLOSED'${mf}
         AND julianday(COALESCE(closed_at, created_at)) >= julianday('now', ?)
       GROUP BY reason
       ORDER BY count DESC`
    : `SELECT COALESCE(exit_reason, 'UNKNOWN') AS reason,
              COUNT(*) AS count,
              COALESCE(SUM(COALESCE(net_pnl_sol, pnl_sol)), 0) AS pnlSol,
              COALESCE(SUM(CASE WHEN COALESCE(net_pnl_sol, pnl_sol) > 0 THEN 1 ELSE 0 END), 0) AS wins
       FROM ${table}
       WHERE state = 'CLOSED'${mf}
       GROUP BY reason
       ORDER BY count DESC`;
  const rows = (sinceModifier ? db.prepare(sql).all(sinceModifier) : db.prepare(sql).all()) as Array<{
    reason: string;
    count: number;
    pnlSol: number;
    wins: number;
  }>;
  return rows.map((r) => ({
    reason: r.reason,
    count: r.count,
    pnlSol: r.pnlSol,
    winRatePct: r.count > 0 ? (r.wins / r.count) * 100 : 0,
  }));
}

export function listBreakers(db: DB, opts: { limit?: number } = {}): BreakerRow[] {
  const limit = clampLimit(opts.limit, 40, 200);
  const rows = db
    .prepare(
      `SELECT type, detail, tripped, at FROM breaker_events ORDER BY rowid DESC LIMIT ?`,
    )
    .all(limit) as Array<{ type: string; detail: string | null; tripped: number; at: string }>;
  return rows.map((r) => ({
    type: r.type,
    detail: r.detail,
    tripped: r.tripped === 1,
    at: r.at,
  }));
}

export interface SlotDist {
  n: number;
  p50: number | null;
  p90: number | null;
}

export interface RacerMigrationRow {
  mint: string;
  signature: string;
  caller: string | null;
  tracked: boolean;
  fireToComplete: number | null;
  completeToMigrate: number | null;
  failedAttempts: number;
  curveBuyBefore: boolean;
  poolBuyAfter: boolean;
  openPrice: number | null;
  reachPrice: number | null;
  unpricedReason: string | null;
  initialQuoteDepthSol: number | null;
  observedQuoteDepthSol: number | null;
  depthThresholdSol: number | null;
  /** Absolute slots: the migrate, the heartbeat at receipt, and the earliest modeled landing slot. */
  migrateSlot: number | null;
  receiptSlot: number | null;
  feasibleSlot: number | null;
  sendReadyUs: number | null;
  at: string;
}

export interface RacerPaperRow {
  mint: string;
  poolAddress: string | null;
  sizeSol: number;
  openPrice: number | null;
  reachPrice: number;
  horizons: Array<{
    sec: number;
    price: number | null;
    quoteSol: number | null;
    pnlOpenPct: number | null;
    pnlReachPct: number | null;
    pnl?: Record<string, number | null>;
    exitPrice?: number | null;
    exitQuoteSol?: number | null;
    exitRealQuoteSol?: number | null;
    exitAtMs?: number;
    rugged?: boolean;
    source?: 'path' | 'rpc';
    fill?: string;
  }>;
  armed: boolean | null;
  liveCostSol: number | null;
  liveCostSource: string | null;
  entryModelVersion: number | null;
  entryQuality: string;
  sameSlotDepletionPct: number | null;
  walletBalanceSol: number | null;
  sizingReason: string | null;
  migrationCaller: string | null;
  entryFeatures: import('../racer/paper.ts').EntryFeatureSnapshot | null;
  entries: Record<string, PoolMark | null>;
  pathResults: Partial<Record<ExitRule, ExitResult>> | null;
  at: string;
}

export interface RacerEntryStat {
  n: number;
  median: number | null;
  winRate: number | null;
}

export interface RacerHorizonStat {
  sec: number;
  n: number;
  medianOpenPct: number | null;
  medianReachPct: number | null;
  winRateOpen: number | null;
  winRateReach: number | null;
  /** Per entry: open, reach, slot1, slot2, … (realistic legs net of live cost). */
  entries: Record<string, RacerEntryStat>;
  headline: {
    mean: number | null;
    meanWithoutBest: number | null;
    ciLow: number | null;
    ciHigh: number | null;
    cumulativeFixedSizePnlSol: number;
    fixedSizePnlCurve: Array<{ mint: string; exitAt: string; pnlSol: number; cumulativePnlSol: number }>;
    fixedSizeCurveUnavailable: number;
    /** Same exits at each track's actual (wallet-sized) size — no repricing needed. */
    cumulativeWalletSizePnlSol: number;
    walletSizePnlCurve: Array<{ mint: string; exitAt: string; pnlSol: number; cumulativePnlSol: number }>;
  };
  cohorts: {
    belowHighDepletion: RacerCohortStat;
    highDepletion: RacerCohortStat;
    unknownDepletion: RacerCohortStat;
    byCaller: Array<{ caller: string; stats: RacerCohortStat }>;
  };
  featureSplits: Array<{ feature: string; cohort: string; stats: RacerCohortStat }>;
  sizeScenario: { sizeSol: number; priced: number; unavailable: number; pnlSol: number; peakConcurrentSol: number; ataRentLockedSol: number; onChainFillAdjustedPnlSol: number | null };
}

export interface RacerCohortStat { n: number; mean: number | null; median: number | null; winRate: number | null; ciLow: number | null; ciHigh: number | null }

export interface RacerSummary {
  curves: { admitted: number; fired: number; open: number; migrated: number; completeNoPool: number; expired: number };
  migrations: { total: number; tracked: number; fired: number; unpriced: Record<string, number>; coverage: Record<string, number> };
  slots: { fireToComplete: SlotDist; completeToMigrate: SlotDist; fireToMigrate: SlotDist };
  competition: { attempts: number; failedAttempts: number; failedPerMigration: number | null; contestedShare: number | null };
  execution: { slot1BuyAttempts: number; slot1Failed: number; observedOnChainFillRate: number | null; offChainDropRate: null;
    slot0BuyVolumeSol: number | null; slot1BuyVolumeSol: number | null; slot0DistinctBuyerOwners: number; slot1DistinctBuyerOwners: number;
    meanFailedNetworkFeeSol: number | null };
  bundles: { curveBuyBeforeShare: number | null; poolBuyAfterShare: number | null };
  topCallers: Array<{ caller: string; wins: number; share: number }>;
  buyLeg: { okRate: number | null; avgMs: number | null };
  paper: {
    tracks: number;
    openPriced: number;
    realisticTracks: number;
    verifiedTracks: number;
    excludedReasons: Record<string, number>;
    highDepletionThresholdPct: number;
    ataRentEstimateSol: number;
    armedShare: number | null;
    /** Tracks whose pool was gone at some horizon (scored −100%). */
    ruggedTracks: number;
    /** Share of realistic tracks whose live cost was measured from +1-slot buyers. */
    measuredCostShare: number | null;
    medianLiveCostSol: number | null;
    /** Wallet sizing: per-track size cohorts, balance/size medians over verified tracks. */
    sizing: { reasons: Record<string, number>; medianWalletBalanceSol: number | null; medianSizeSol: number | null };
    horizons: RacerHorizonStat[];
    pathRules: Array<{
      rule: ExitRule;
      n: number;
      unpriced: number;
      /** Verified tracks recorded before path capture existed (or lost to a restart): no path, not an unpriced exit. */
      notCaptured: number;
      /** Of n: fills on an unchanged pool (no later trade on a continuous stream). */
      unchangedFills: number;
      result: RacerCohortStat;
      sameTradeTimeBaseline: RacerCohortStat;
    }>;
    evaluation: { verifiedCompleted: number; exploratoryRows: number; reservedLaterRows: number; selectionReady: boolean; validated: false };
  };
  /**
   * Latency chain, labelled by provenance: `measured` (this racer), `proxy`
   * (other bots' on-chain behaviour), `assumption` (config), `live` (trader
   * latency samples in this DB — empty while the trader is stopped).
   */
  latency: RacerLatency;
  lastMigrateAt: string | null;
  lastCurveAt: string | null;
}

function quantile(sorted: number[], q: number): number | null {
  if (sorted.length === 0) return null;
  const idx = (sorted.length - 1) * q;
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  return sorted[lo]! + (sorted[hi]! - sorted[lo]!) * (idx - lo);
}

const HIGH_SAME_SLOT_DEPLETION_PCT = 90;

function cohortStat(values: number[]): RacerCohortStat {
  const sorted = [...values].sort((a, b) => a - b);
  const [ciLow, ciHigh] = bootstrapMeanCi(sorted, 31);
  return {
    n: values.length,
    mean: values.length ? values.reduce((a, b) => a + b, 0) / values.length : null,
    median: quantile(sorted, 0.5),
    winRate: values.length ? values.filter((v) => v > 0).length / values.length : null,
    ciLow, ciHigh,
  };
}

/** Predeclared decision-time bins. Missing snapshots never inherit a favorable cohort. */
function featureBins(row: RacerPaperRow): Array<[string, string]> {
  const f = row.entryFeatures;
  if (!f || f.version !== 1) return [['snapshot', 'unavailable']];
  const bin = (n: number | null, threshold: number, units: string): string => n === null ? 'unknown' : n < threshold ? `<${threshold}${units}` : `≥${threshold}${units}`;
  return [
    ['fire→complete', bin(f.fireToCompleteSlots, 3, ' slots')],
    ['complete→migrate', bin(f.completeToMigrateSlots, 3, ' slots')],
    ['caller', f.caller ?? 'unknown'],
    ['quote vault', bin(f.quoteVaultSol, 10, ' SOL')],
    ['token program', f.tokenProgram ?? 'unknown'],
    ['depletion known at decision', bin(f.sameSlotDepletionPct, HIGH_SAME_SLOT_DEPLETION_PCT, '%')],
    ['failed migrate attempts known', f.knownFailedMigrateAttempts === 0 ? '0' : '>0'],
  ];
}

/** Stable percentile bootstrap for the mean. Null for fewer than two outcomes. */
function bootstrapMeanCi(values: number[], seed: number): [number | null, number | null] {
  if (values.length < 2) return [null, null];
  let state = seed >>> 0;
  const samples: number[] = [];
  for (let i = 0; i < 2_000; i++) {
    let sum = 0;
    for (let j = 0; j < values.length; j++) {
      state = (Math.imul(state, 1_664_525) + 1_013_904_223) >>> 0;
      // Index from the HIGH bits: an LCG's low bits cycle with a tiny period
      // (state % 4 repeats every 4 draws), which made every resample of n=4
      // contain each value exactly once and collapsed the interval to the mean.
      sum += values[Math.floor((state / 4_294_967_296) * values.length)]!;
    }
    samples.push(sum / values.length);
  }
  samples.sort((a, b) => a - b);
  return [quantile(samples, 0.025), quantile(samples, 0.975)];
}

export interface RacerLatency {
  detection: { racerSlots: SlotDist; liveMs: SlotDist; liveSlots: SlotDist };
  sending: {
    /** Racer probe: landed-migrate frame receipt → signed buy bytes (ms; never sent). */
    sendReadyMs: SlotDist;
    stagesMs: { state: SlotDist; build: SlotDist; sign: SlotDist };
    /** Share of probes built from a pool synthesized at admission (no push yet). */
    synthesizedShare: number | null;
    /** Audit of synthesized builds against the real pool: identical instruction accounts. */
    synthChecks: number;
    synthMatchRate: number | null;
    /** |real - synthesized| virtualQuoteReserves, lamports. */
    synthVDeltaLamports: SlotDist;
    liveDetectToSendMs: SlotDist;
  };
  landing: {
    modeledEntrySlot: number;
    /** feasible_slot − migrate slot (measured send-ready + one-way, floored at +1). */
    feasibleOffsetSlots: SlotDist;
    /** Share whose unfloored estimate was still the migrate slot (it may still have been open). */
    migrateSlotStillOpenShare: number | null;
    firstOutsideBuySlots: SlotDist;
    sameSlotBuyShare: number | null;
    onChainFillRate: number | null;
    liveEntryLandSlots: SlotDist;
    liveEntryConfirmMs: SlotDist;
  };
  exit: {
    assumedExitLandMs: number | null;
    /** Marked horizons priced from stream states (no RPC), and the RPC fallbacks. */
    horizonsFromStream: number;
    horizonsFromRpc: number;
    /** Of stream-priced horizons: the sell filled in the next slot vs on an unchanged pool. */
    horizonNextSlotFills: number;
    horizonUnchangedFills: number;
    unchangedFillShare: number | null;
    liveExitTriggerToSendMs: SlotDist;
    liveExitSendToLandedMs: SlotDist;
    liveExitLandSlots: SlotDist;
  };
}

function synthStats(db: DB): { synthesizedShare: number | null; synthChecks: number; synthMatchRate: number | null; synthVDeltaLamports: SlotDist } {
  const vals = (kind: string) => (db.prepare(`SELECT latency_ms AS v FROM latency_samples WHERE kind = ?`).all(kind) as Array<{ v: number }>).map((r) => r.v);
  const src = vals('racer_send_pool_synth');
  const match = vals('racer_pool_synth_match');
  return {
    synthesizedShare: src.length ? src.filter((v) => v === 1).length / src.length : null,
    synthChecks: match.length,
    synthMatchRate: match.length ? match.filter((v) => v === 1).length / match.length : null,
    synthVDeltaLamports: slotDist(vals('racer_pool_synth_vdelta').map((v) => Math.abs(v))),
  };
}

function latencyKind(db: DB, kind: string, scale = 1): SlotDist {
  const rows = db.prepare(`SELECT latency_ms AS v FROM latency_samples WHERE kind = ?`).all(kind) as Array<{ v: number }>;
  return slotDist(rows.map((r) => r.v * scale));
}

function slotDist(values: Array<number | null>): SlotDist {
  const v = values.filter((x): x is number => x !== null && Number.isFinite(x)).sort((a, b) => a - b);
  return { n: v.length, p50: quantile(v, 0.5), p90: quantile(v, 0.9) };
}

export function listRacerMigrations(db: DB, opts: { limit?: number } = {}): RacerMigrationRow[] {
  const limit = clampLimit(opts.limit, 100, 500);
  const rows = db
    .prepare(
      `SELECT m.mint, m.signature, m.caller, m.tracked, m.fire_slot AS fireSlot, m.complete_slot AS completeSlot,
              m.migrate_slot AS migrateSlot, m.curve_buy_before AS curveBuyBefore, m.pool_buy_after AS poolBuyAfter,
              m.open_price AS openPrice, m.reach_price AS reachPrice, m.unpriced_reason AS unpricedReason,
              m.initial_quote_depth_sol AS initialQuoteDepthSol, m.observed_quote_depth_sol AS observedQuoteDepthSol,
              m.depth_threshold_sol AS depthThresholdSol, m.created_at AS at,
              m.receipt_slot AS receiptSlot, m.feasible_slot AS feasibleSlot, m.send_ready_us AS sendReadyUs,
              (SELECT COUNT(*) FROM racer_migrate_txs t WHERE t.mint = m.mint AND t.landed = 0) AS failedAttempts
       FROM racer_migrations m ORDER BY m.rowid DESC LIMIT ?`,
    )
    .all(limit) as Array<{
    mint: string;
    signature: string;
    caller: string | null;
    tracked: number;
    fireSlot: number | null;
    completeSlot: number | null;
    migrateSlot: number | null;
    curveBuyBefore: number;
    poolBuyAfter: number;
    openPrice: number | null;
    reachPrice: number | null;
    unpricedReason: string | null;
    initialQuoteDepthSol: number | null;
    observedQuoteDepthSol: number | null;
    depthThresholdSol: number | null;
    at: string;
    receiptSlot: number | null;
    feasibleSlot: number | null;
    sendReadyUs: number | null;
    failedAttempts: number;
  }>;
  return rows.map((r) => ({
    mint: r.mint,
    signature: r.signature,
    caller: r.caller,
    tracked: r.tracked === 1,
    fireToComplete: r.fireSlot !== null && r.completeSlot !== null ? r.completeSlot - r.fireSlot : null,
    completeToMigrate: r.completeSlot !== null && r.migrateSlot !== null ? r.migrateSlot - r.completeSlot : null,
    failedAttempts: r.failedAttempts,
    curveBuyBefore: r.curveBuyBefore === 1,
    poolBuyAfter: r.poolBuyAfter === 1,
    openPrice: r.openPrice,
    reachPrice: r.reachPrice,
    unpricedReason: r.unpricedReason,
    initialQuoteDepthSol: r.initialQuoteDepthSol,
    observedQuoteDepthSol: r.observedQuoteDepthSol,
    depthThresholdSol: r.depthThresholdSol,
    migrateSlot: r.migrateSlot,
    receiptSlot: r.receiptSlot,
    feasibleSlot: r.feasibleSlot,
    sendReadyUs: r.sendReadyUs,
    at: r.at,
  }));
}

export function listRacerPaper(db: DB, opts: { limit?: number; all?: boolean } = {}): RacerPaperRow[] {
  const limit = opts.all ? 2_147_483_647 : clampLimit(opts.limit, 100, 5000);
  const rows = db
    .prepare(
      `SELECT p.mint, p.pool_address AS poolAddress, p.size_sol AS sizeSol, p.open_price AS openPrice,
              p.reach_price AS reachPrice, p.horizons_json AS horizonsJson, p.entries_json AS entriesJson, p.armed,
              p.live_cost_sol AS liveCostSol, p.live_cost_source AS liveCostSource,
              p.entry_model_version AS entryModelVersion, p.entry_quality AS entryQuality,
              p.same_slot_depletion_pct AS sameSlotDepletionPct, p.entry_features_json AS entryFeaturesJson,
              p.path_results_json AS pathResultsJson,
              p.wallet_balance_sol AS walletBalanceSol, p.sizing_reason AS sizingReason,
              m.caller AS migrationCaller, p.created_at AS at
       FROM racer_paper_tracks p LEFT JOIN racer_migrations m ON m.mint = p.mint ORDER BY p.rowid DESC LIMIT ?`,
    )
    .all(limit) as Array<{
    mint: string;
    poolAddress: string | null;
    sizeSol: number;
    openPrice: number | null;
    reachPrice: number;
    horizonsJson: string;
    entriesJson: string | null;
    armed: number | null;
    liveCostSol: number | null;
    liveCostSource: string | null;
    entryModelVersion: number | null;
    entryQuality: string | null;
    sameSlotDepletionPct: number | null;
    entryFeaturesJson: string | null;
    pathResultsJson: string | null;
    walletBalanceSol: number | null;
    sizingReason: string | null;
    migrationCaller: string | null;
    at: string;
  }>;
  return rows.map((r) => {
    let horizons: RacerPaperRow['horizons'] = [];
    try {
      horizons = JSON.parse(r.horizonsJson) as RacerPaperRow['horizons'];
    } catch {
      horizons = [];
    }
    return {
      mint: r.mint,
      poolAddress: r.poolAddress,
      sizeSol: r.sizeSol,
      openPrice: r.openPrice,
      reachPrice: r.reachPrice,
      horizons,
      entries: (parseJson(r.entriesJson) ?? {}) as Record<string, PoolMark | null>,
      armed: r.armed === null ? null : r.armed === 1,
      liveCostSol: r.liveCostSol,
      liveCostSource: r.liveCostSource,
      entryModelVersion: r.entryModelVersion,
      entryQuality: r.entryModelVersion === 2 ? (r.entryQuality ?? 'missing-anchor') : 'legacy-unverified',
      sameSlotDepletionPct: r.sameSlotDepletionPct,
      walletBalanceSol: r.walletBalanceSol,
      sizingReason: r.sizingReason,
      entryFeatures: parseJson(r.entryFeaturesJson) as import('../racer/paper.ts').EntryFeatureSnapshot | null,
      pathResults: parseJson(r.pathResultsJson) as Partial<Record<ExitRule, ExitResult>> | null,
      migrationCaller: r.migrationCaller,
      at: r.at,
    };
  });
}

function racerLatency(
  db: DB,
  opts: { exitLandMs?: number; modeledEntrySlot?: number },
  onChainFillRate: number | null,
  pathRules: Array<{ rule: string; n: number; unchangedFills: number }>,
  paperRows: RacerPaperRow[] = [],
): RacerLatency {
  const marked = paperRows.flatMap((r) => r.horizons.filter((h) => h.price !== null || h.rugged));
  const fromStream = marked.filter((h) => h.source === 'path');
  // First outside pool buy after each migrate, in slots (0 = inside the migrate slot).
  const first = db
    .prepare(
      `SELECT MIN(a.slot) - m.migrate_slot AS offset
       FROM racer_pool_buy_attempts a JOIN racer_migrations m ON m.pool_address = a.pool_address
       WHERE a.ok = 1 AND m.migrate_slot IS NOT NULL AND a.slot >= m.migrate_slot
       GROUP BY m.mint`,
    )
    .all() as Array<{ offset: number }>;
  const time = pathRules.find((r) => r.rule === 'time300');
  const feas = db
    .prepare(
      `SELECT feasible_slot - migrate_slot AS off, feasible_slot_raw - migrate_slot AS rawOff
       FROM racer_migrations WHERE feasible_slot IS NOT NULL AND migrate_slot IS NOT NULL`,
    )
    .all() as Array<{ off: number; rawOff: number | null }>;
  return {
    detection: {
      racerSlots: latencyKind(db, 'racer_detect_slots'),
      liveMs: latencyKind(db, 'detection'),
      liveSlots: latencyKind(db, 'detection_slots'),
    },
    sending: {
      sendReadyMs: latencyKind(db, 'racer_send_ready_us', 1 / 1000),
      stagesMs: {
        state: latencyKind(db, 'racer_send_state_us', 1 / 1000),
        build: latencyKind(db, 'racer_send_build_us', 1 / 1000),
        sign: latencyKind(db, 'racer_send_sign_us', 1 / 1000),
      },
      liveDetectToSendMs: latencyKind(db, 'detect_to_send'),
      ...synthStats(db),
    },
    landing: {
      modeledEntrySlot: opts.modeledEntrySlot ?? 1,
      feasibleOffsetSlots: slotDist(feas.map((r) => r.off)),
      migrateSlotStillOpenShare: feas.length ? feas.filter((r) => r.rawOff !== null && r.rawOff <= 0).length / feas.length : null,
      firstOutsideBuySlots: slotDist(first.map((r) => r.offset)),
      sameSlotBuyShare: first.length ? first.filter((r) => r.offset === 0).length / first.length : null,
      onChainFillRate,
      liveEntryLandSlots: latencyKind(db, 'entry_land_slots'),
      liveEntryConfirmMs: latencyKind(db, 'entry_confirm'),
    },
    exit: {
      assumedExitLandMs: opts.exitLandMs ?? null,
      horizonsFromStream: fromStream.length,
      horizonsFromRpc: marked.filter((h) => h.source === 'rpc').length,
      horizonNextSlotFills: fromStream.filter((h) => h.fill === 'next-slot').length,
      horizonUnchangedFills: fromStream.filter((h) => h.fill === 'unchanged').length,
      unchangedFillShare: time && time.n > 0 ? time.unchangedFills / time.n : null,
      liveExitTriggerToSendMs: latencyKind(db, 'exit_trigger_to_send'),
      liveExitSendToLandedMs: latencyKind(db, 'exit_send_to_landed'),
      liveExitLandSlots: latencyKind(db, 'exit_land_slots'),
    },
  };
}

export function getRacerSummary(
  db: DB,
  opts: { proposedSizeSol?: number; ataRentSol?: number; exitLandMs?: number; modeledEntrySlot?: number } = {},
): RacerSummary {
  const proposedSizeSol = opts.proposedSizeSol ?? 0.1;
  const ataRentSol = opts.ataRentSol ?? 0.00203928;
  const c = db
    .prepare(
      `SELECT COUNT(*) AS admitted,
              SUM(CASE WHEN fire_slot IS NOT NULL THEN 1 ELSE 0 END) AS fired,
              SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS open,
              SUM(CASE WHEN outcome = 'migrated' THEN 1 ELSE 0 END) AS migrated,
              SUM(CASE WHEN outcome = 'complete-no-pool' THEN 1 ELSE 0 END) AS completeNoPool,
              SUM(CASE WHEN outcome = 'expired' THEN 1 ELSE 0 END) AS expired,
              MAX(updated_at) AS lastCurveAt
       FROM racer_curves`,
    )
    .get() as Record<string, number | string | null>;
  const migs = db
    .prepare(
      `SELECT tracked, fire_slot AS fireSlot, complete_slot AS completeSlot, migrate_slot AS migrateSlot,
              curve_buy_before AS cb, pool_buy_after AS pb, created_at AS at
       FROM racer_migrations`,
    )
    .all() as Array<{ tracked: number; fireSlot: number | null; completeSlot: number | null; migrateSlot: number | null; cb: number; pb: number; at: string }>;
  const total = migs.length;
  const share = (n: number) => (total > 0 ? n / total : null);
  const failed = db
    .prepare(
      `SELECT COUNT(*) AS attempts,
              SUM(CASE WHEN t.landed = 0 THEN 1 ELSE 0 END) AS failed
       FROM racer_migrate_txs t WHERE t.mint IN (SELECT mint FROM racer_migrations)`,
    )
    .get() as { attempts: number; failed: number | null };
  const buys = db.prepare(`SELECT COUNT(*) AS attempts, SUM(CASE WHEN a.ok = 0 THEN 1 ELSE 0 END) AS failed,
    AVG(CASE WHEN a.ok = 0 THEN a.network_fee_sol END) AS meanFailedFee
    FROM racer_pool_buy_attempts a JOIN racer_migrations m ON m.pool_address = a.pool_address
    JOIN racer_paper_tracks p ON p.mint = m.mint AND p.entry_model_version = 2 AND p.entry_quality = 'verified'
    WHERE m.migrate_slot IS NOT NULL AND a.slot = m.migrate_slot + 1`).get() as { attempts: number; failed: number | null; meanFailedFee: number | null };
  const onChainFillRate = buys.attempts ? (buys.attempts - (buys.failed ?? 0)) / buys.attempts : null;
  const buyFlow = db.prepare(`SELECT a.slot - m.migrate_slot AS offset,
    SUM(a.quote_volume_sol) AS volumeSol, COUNT(DISTINCT CASE WHEN a.ok = 1 THEN a.buyer_owner END) AS buyers
    FROM racer_pool_buy_attempts a JOIN racer_migrations m ON m.pool_address = a.pool_address
    WHERE m.migrate_slot IS NOT NULL AND a.slot IN (m.migrate_slot, m.migrate_slot + 1)
    GROUP BY a.slot - m.migrate_slot`).all() as Array<{ offset: number; volumeSol: number | null; buyers: number }>;
  const flow0 = buyFlow.find((x) => x.offset === 0);
  const flow1 = buyFlow.find((x) => x.offset === 1);
  const contested = db
    .prepare(
      `SELECT COUNT(DISTINCT t.mint) AS n FROM racer_migrate_txs t
       WHERE t.landed = 0 AND t.mint IN (SELECT mint FROM racer_migrations)`,
    )
    .get() as { n: number };
  const unpricedRows = db
    .prepare(`SELECT unpriced_reason AS r, COUNT(*) AS n FROM racer_migrations WHERE unpriced_reason IS NOT NULL GROUP BY unpriced_reason`)
    .all() as Array<{ r: string; n: number }>;
  const unpriced: Record<string, number> = {};
  for (const u of unpricedRows) unpriced[u.r] = u.n;
  const coverageRows = db.prepare(`SELECT m.unpriced_reason AS reason, p.entry_model_version AS version,
      p.entry_quality AS quality, COUNT(*) AS n FROM racer_migrations m
      LEFT JOIN racer_paper_tracks p ON p.mint = m.mint
      GROUP BY m.unpriced_reason, p.entry_model_version, p.entry_quality`).all() as Array<{
        reason: string | null; version: number | null; quality: string | null; n: number;
      }>;
  const coverage: Record<string, number> = {};
  for (const row of coverageRows) {
    const reason = row.reason ? `unpriced:${row.reason}` : row.version === 2 && row.quality === 'verified'
      ? 'verified' : row.quality ?? (row.version === null ? 'missing-track' : 'missing-anchor');
    coverage[reason] = (coverage[reason] ?? 0) + row.n;
  }
  const callers = db
    .prepare(`SELECT caller, COUNT(*) AS wins FROM racer_migrations WHERE caller IS NOT NULL GROUP BY caller ORDER BY wins DESC LIMIT 5`)
    .all() as Array<{ caller: string; wins: number }>;
  const leg = db
    .prepare(
      `SELECT AVG(CASE WHEN buy_leg_ok = 1 THEN 1.0 ELSE 0.0 END) AS okRate, AVG(buy_leg_ms) AS ms
       FROM racer_curves WHERE buy_leg_ok IS NOT NULL`,
    )
    .get() as { okRate: number | null; ms: number | null };

  const paperRows = listRacerPaper(db, { all: true });
  const verifiedRows = paperRows.filter((r) => r.entryModelVersion === 2 && r.entryQuality === 'verified');
  const longestConfiguredHold = Math.max(0, ...paperRows.flatMap((r) => r.horizons.map((h) => h.sec)));
  const completedVerified = verifiedRows.filter((r) => typeof r.horizons.find((h) => h.sec === longestConfiguredHold)?.pnl?.slot1mid === 'number')
    .sort((a, b) => a.at.localeCompare(b.at) || a.mint.localeCompare(b.mint));
  const exploratory = new Set(completedVerified.slice(0, 300).map((r) => r.mint));
  const excludedReasons: Record<string, number> = {};
  for (const row of paperRows) {
    if (row.entryModelVersion === 2 && row.entryQuality === 'verified') continue;
    excludedReasons[row.entryQuality] = (excludedReasons[row.entryQuality] ?? 0) + 1;
  }
  // sec → entry name → pnl values. Rows written before realistic entries carry
  // only pnlOpenPct / pnlReachPct.
  const bySec = new Map<number, Map<string, number[]>>();
  const bySecRows = new Map<number, RacerPaperRow[]>();
  for (const row of verifiedRows) {
    for (const h of row.horizons) {
      const secRows = bySecRows.get(h.sec) ?? [];
      secRows.push(row);
      bySecRows.set(h.sec, secRows);
      const b = bySec.get(h.sec) ?? new Map<string, number[]>();
      const pnl: Record<string, number | null | undefined> = h.pnl ?? { open: h.pnlOpenPct, reach: h.pnlReachPct };
      for (const [name, v] of Object.entries(pnl)) {
        if (v === null || v === undefined || !Number.isFinite(v)) continue;
        const arr = b.get(name) ?? [];
        arr.push(v);
        b.set(name, arr);
      }
      bySec.set(h.sec, b);
    }
  }
  const winRate = (a: number[]) => (a.length ? a.filter((x) => x > 0).length / a.length : null);
  const horizons: RacerHorizonStat[] = [...bySec.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([sec, b]) => {
      const entries: Record<string, RacerEntryStat> = {};
      for (const [name, vals] of b) {
        const sorted = [...vals].sort((x, y) => x - y);
        entries[name] = { n: sorted.length, median: quantile(sorted, 0.5), winRate: winRate(sorted) };
      }
      const scored = (bySecRows.get(sec) ?? []).map((row) => ({
        row,
        value: row.horizons.find((h) => h.sec === sec)?.pnl?.slot1mid,
      })).filter((x): x is { row: RacerPaperRow; value: number } => typeof x.value === 'number' && Number.isFinite(x.value));
      const values = scored.map((x) => x.value);
      const sortedBest = [...values].sort((a, b) => b - a);
      const [ciLow, ciHigh] = bootstrapMeanCi(values, sec);
      let sizePnlSol = 0;
      let sizePriced = 0;
      let sizeUnavailable = 0;
      const capitalEvents: Array<{ at: number; delta: number }> = [];
      for (const { row } of scored) {
        const h = row.horizons.find((x) => x.sec === sec)!;
        const entry = row.entries.slot1mid;
        let pct: number | null = null;
        if (h.rugged) pct = -100;
        else if (entry && h.exitPrice != null && h.exitQuoteSol != null) {
          pct = paperRoundTripPct(entry, { price: h.exitPrice, quoteSol: h.exitQuoteSol,
            ...(h.exitRealQuoteSol != null ? { realQuoteSol: h.exitRealQuoteSol } : {}) }, proposedSizeSol);
        }
        if (pct === null) { sizeUnavailable++; continue; }
        sizePriced++;
        sizePnlSol += proposedSizeSol * pct / 100 - (row.liveCostSol ?? 0);
        const start = Date.parse(row.at.replace(' ', 'T') + (row.at.includes('Z') ? '' : 'Z'));
        capitalEvents.push({ at: start, delta: proposedSizeSol + ataRentSol });
        capitalEvents.push({ at: start + sec * 1_000, delta: -(proposedSizeSol + ataRentSol) });
      }
      capitalEvents.sort((a, b) => a.at - b.at || a.delta - b.delta);
      let capital = 0;
      let peakConcurrentSol = 0;
      for (const event of capitalEvents) { capital += event.delta; peakConcurrentSol = Math.max(peakConcurrentSol, capital); }
      let cumulativePnlSol = 0;
      const exitTime = (row: RacerPaperRow): number => row.horizons.find((h) => h.sec === sec)?.exitAtMs ??
        Date.parse(row.at.replace(' ', 'T') + (row.at.includes('Z') ? '' : 'Z')) + sec * 1_000;
      const fixedRows = scored.flatMap(({ row, value }) => {
        if (row.sizeSol === 0.1) return [{ row, value }];
        const h = row.horizons.find((x) => x.sec === sec)!;
        const entry = row.entries.slot1mid;
        if (!entry) return [];
        const fixed = h.rugged ? -100 : h.exitPrice != null && h.exitQuoteSol != null
          ? paperRoundTripPct(entry, { price: h.exitPrice, quoteSol: h.exitQuoteSol,
            ...(h.exitRealQuoteSol != null ? { realQuoteSol: h.exitRealQuoteSol } : {}) }, 0.1) : null;
        return fixed === null ? [] : [{ row, value: fixed - (row.liveCostSol ?? 0) / 0.1 * 100 }];
      });
      const fixedSizePnlCurve = [...fixedRows].sort((a, b) =>
        exitTime(a.row) - exitTime(b.row) || a.row.mint.localeCompare(b.row.mint),
      ).map(({ row, value }) => {
        const pnlSol = 0.1 * value / 100;
        cumulativePnlSol += pnlSol;
        return { mint: row.mint, exitAt: new Date(exitTime(row)).toISOString(), pnlSol, cumulativePnlSol };
      });
      // Wallet-sized curve: each scored value is already net of cost at that
      // track's own size, so no repricing — legacy 0.1 rows join unchanged.
      let cumulativeWalletPnlSol = 0;
      const walletSizePnlCurve = [...scored].sort((a, b) =>
        exitTime(a.row) - exitTime(b.row) || a.row.mint.localeCompare(b.row.mint),
      ).map(({ row, value }) => {
        const pnlSol = row.sizeSol * value / 100;
        cumulativeWalletPnlSol += pnlSol;
        return { mint: row.mint, exitAt: new Date(exitTime(row)).toISOString(), pnlSol, cumulativePnlSol: cumulativeWalletPnlSol };
      });
      const callers = new Map<string, number[]>();
      const featureGroups = new Map<string, { feature: string; cohort: string; values: number[] }>();
      for (const { row, value } of scored) {
        const caller = row.migrationCaller ?? 'unknown';
        const arr = callers.get(caller) ?? [];
        arr.push(value);
        callers.set(caller, arr);
        if (!exploratory.has(row.mint)) continue;
        for (const [feature, cohort] of featureBins(row)) {
          const key = `${feature}:${cohort}`;
          const group = featureGroups.get(key) ?? { feature, cohort, values: [] };
          group.values.push(value);
          featureGroups.set(key, group);
        }
      }
      return {
        sec,
        n: entries.reach?.n ?? 0,
        medianOpenPct: entries.open?.median ?? null,
        medianReachPct: entries.reach?.median ?? null,
        winRateOpen: entries.open?.winRate ?? null,
        winRateReach: entries.reach?.winRate ?? null,
        entries,
        headline: {
          mean: cohortStat(values).mean,
          meanWithoutBest: sortedBest.length > 1 ? cohortStat(sortedBest.slice(1)).mean : null,
          ciLow,
          ciHigh,
          cumulativeFixedSizePnlSol: cumulativePnlSol,
          fixedSizePnlCurve,
          fixedSizeCurveUnavailable: scored.length - fixedRows.length,
          cumulativeWalletSizePnlSol: cumulativeWalletPnlSol,
          walletSizePnlCurve,
        },
        cohorts: {
          belowHighDepletion: cohortStat(scored.filter((x) => x.row.sameSlotDepletionPct !== null && x.row.sameSlotDepletionPct < HIGH_SAME_SLOT_DEPLETION_PCT).map((x) => x.value)),
          highDepletion: cohortStat(scored.filter((x) => x.row.sameSlotDepletionPct !== null && x.row.sameSlotDepletionPct >= HIGH_SAME_SLOT_DEPLETION_PCT).map((x) => x.value)),
          unknownDepletion: cohortStat(scored.filter((x) => x.row.sameSlotDepletionPct === null).map((x) => x.value)),
          byCaller: [...callers.entries()].map(([caller, vals]) => ({ caller, stats: cohortStat(vals) })).sort((a, b) => b.stats.n - a.stats.n),
        },
        featureSplits: [...featureGroups.values()].map((g) => ({ feature: g.feature, cohort: g.cohort, stats: cohortStat(g.values) }))
          .sort((a, b) => a.feature.localeCompare(b.feature) || a.cohort.localeCompare(b.cohort)),
        sizeScenario: { sizeSol: proposedSizeSol, priced: sizePriced, unavailable: sizeUnavailable,
          pnlSol: sizePnlSol, peakConcurrentSol, ataRentLockedSol: sizePriced * ataRentSol,
          onChainFillAdjustedPnlSol: onChainFillRate === null || ((buys.failed ?? 0) > 0 && buys.meanFailedFee === null) ? null : sizePnlSol * onChainFillRate -
            sizePriced * (1 - onChainFillRate) * (buys.meanFailedFee ?? 0) },
      };
    });
  // `armed` is set exactly when realistic entries were priced (even before any horizon lands).
  const realisticRows = paperRows.filter((r) => r.armed !== null);
  const longestHold = Math.max(0, ...horizons.map((h) => h.sec));
  const pathEligible = verifiedRows.filter((r) => exploratory.has(r.mint) && typeof r.horizons.find((h) => h.sec === longestHold)?.pnl?.slot1mid === 'number');
  const pathRules = EXIT_RULES.map((rule) => {
    const paired = pathEligible.flatMap((row) => {
      const value = row.pathResults?.[rule]?.pnlPct;
      const baseline = row.horizons.find((h) => h.sec === longestHold)?.pnl?.slot1mid;
      return typeof value === 'number' && Number.isFinite(value) && typeof baseline === 'number'
        ? [{ value, baseline, unchanged: row.pathResults?.[rule]?.fill === 'unchanged' }]
        : [];
    });
    const captured = pathEligible.filter((row) => row.pathResults !== null).length;
    return { rule, n: paired.length, unpriced: captured - paired.length, notCaptured: pathEligible.length - captured,
      unchangedFills: paired.filter((p) => p.unchanged).length,
      result: cohortStat(paired.map((p) => p.value)), sameTradeTimeBaseline: cohortStat(paired.map((p) => p.baseline)) };
  });

  return {
    curves: {
      admitted: Number(c.admitted ?? 0),
      fired: Number(c.fired ?? 0),
      open: Number(c.open ?? 0),
      migrated: Number(c.migrated ?? 0),
      completeNoPool: Number(c.completeNoPool ?? 0),
      expired: Number(c.expired ?? 0),
    },
    migrations: {
      total,
      tracked: migs.filter((m) => m.tracked === 1).length,
      fired: migs.filter((m) => m.fireSlot !== null).length,
      unpriced,
      coverage,
    },
    slots: {
      fireToComplete: slotDist(migs.map((m) => (m.fireSlot !== null && m.completeSlot !== null ? m.completeSlot - m.fireSlot : null))),
      completeToMigrate: slotDist(migs.map((m) => (m.completeSlot !== null && m.migrateSlot !== null ? m.migrateSlot - m.completeSlot : null))),
      fireToMigrate: slotDist(migs.map((m) => (m.fireSlot !== null && m.migrateSlot !== null ? m.migrateSlot - m.fireSlot : null))),
    },
    competition: {
      attempts: failed.attempts,
      failedAttempts: failed.failed ?? 0,
      failedPerMigration: total > 0 ? (failed.failed ?? 0) / total : null,
      contestedShare: share(contested.n),
    },
    execution: { slot1BuyAttempts: buys.attempts, slot1Failed: buys.failed ?? 0,
      observedOnChainFillRate: onChainFillRate, offChainDropRate: null,
      slot0BuyVolumeSol: flow0?.volumeSol ?? null, slot1BuyVolumeSol: flow1?.volumeSol ?? null,
      slot0DistinctBuyerOwners: flow0?.buyers ?? 0, slot1DistinctBuyerOwners: flow1?.buyers ?? 0,
      meanFailedNetworkFeeSol: buys.meanFailedFee },
    bundles: {
      curveBuyBeforeShare: share(migs.filter((m) => m.cb === 1).length),
      poolBuyAfterShare: share(migs.filter((m) => m.pb === 1).length),
    },
    topCallers: callers.map((x) => ({ caller: x.caller, wins: x.wins, share: total > 0 ? x.wins / total : 0 })),
    buyLeg: { okRate: leg.okRate, avgMs: leg.ms },
    paper: {
      tracks: paperRows.length,
      openPriced: paperRows.filter((r) => r.openPrice !== null).length,
      realisticTracks: realisticRows.length,
      verifiedTracks: verifiedRows.length,
      excludedReasons,
      highDepletionThresholdPct: HIGH_SAME_SLOT_DEPLETION_PCT,
      ataRentEstimateSol: ataRentSol,
      armedShare: realisticRows.length ? realisticRows.filter((r) => r.armed === true).length / realisticRows.length : null,
      ruggedTracks: paperRows.filter((r) => r.horizons.some((h) => h.rugged)).length,
      measuredCostShare: realisticRows.length ? realisticRows.filter((r) => r.liveCostSource === 'measured').length / realisticRows.length : null,
      medianLiveCostSol: quantile(
        realisticRows
          .filter((r) => r.liveCostSource === 'measured' && r.liveCostSol !== null)
          .map((r) => r.liveCostSol!)
          .sort((a, b) => a - b),
        0.5,
      ),
      sizing: {
        reasons: verifiedRows.reduce<Record<string, number>>((acc, r) => {
          const k = r.sizingReason ?? 'legacy-fixed';
          acc[k] = (acc[k] ?? 0) + 1;
          return acc;
        }, {}),
        medianWalletBalanceSol: quantile(
          verifiedRows.filter((r) => r.walletBalanceSol !== null).map((r) => r.walletBalanceSol!).sort((a, b) => a - b),
          0.5,
        ),
        medianSizeSol: quantile(
          verifiedRows.map((r) => r.sizeSol).sort((a, b) => a - b),
          0.5,
        ),
      },
      horizons,
      pathRules,
      evaluation: { verifiedCompleted: completedVerified.length, exploratoryRows: Math.min(300, completedVerified.length),
        reservedLaterRows: Math.max(0, completedVerified.length - 300), selectionReady: completedVerified.length >= 300, validated: false },
    },
    latency: racerLatency(db, opts, onChainFillRate, pathRules, paperRows),
    lastMigrateAt: migs.length ? migs.reduce((a, m) => (m.at > a ? m.at : a), migs[0]!.at) : null,
    lastCurveAt: (c.lastCurveAt as string | null) ?? null,
  };
}

export function listCheckFailRates(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): Array<{ checkId: string; pass: number; fail: number; unknown: number; failRatePct: number }> {
  const mod = rangeToModifier(opts.range);
  const sql = mod
    ? `SELECT check_id AS checkId,
              SUM(CASE WHEN status = 'pass' THEN 1 ELSE 0 END) AS pass,
              SUM(CASE WHEN status = 'fail' THEN 1 ELSE 0 END) AS fail,
              SUM(CASE WHEN status = 'unknown' THEN 1 ELSE 0 END) AS unknown
       FROM candidate_check_results
       WHERE julianday(created_at) >= julianday('now', ?)
       GROUP BY check_id
       ORDER BY fail DESC`
    : `SELECT check_id AS checkId,
              SUM(CASE WHEN status = 'pass' THEN 1 ELSE 0 END) AS pass,
              SUM(CASE WHEN status = 'fail' THEN 1 ELSE 0 END) AS fail,
              SUM(CASE WHEN status = 'unknown' THEN 1 ELSE 0 END) AS unknown
       FROM candidate_check_results
       GROUP BY check_id
       ORDER BY fail DESC`;
  const rows = (mod ? db.prepare(sql).all(mod) : db.prepare(sql).all()) as Array<{
    checkId: string;
    pass: number;
    fail: number;
    unknown: number;
  }>;
  return rows.map((r) => {
    const total = r.pass + r.fail + r.unknown;
    return {
      ...r,
      failRatePct: total > 0 ? (r.fail / total) * 100 : 0,
    };
  });
}

export type VetoReasonCategory = 'unknown' | 'hard_fail' | 'low_score' | 'none';

export interface VetoReasonRow {
  reason: string;
  category: VetoReasonCategory;
  count: number;
  pct: number;
}

export interface VetoReasonBreakdown {
  totalVetoed: number;
  /** By the single primary (first) veto reason per candidate — sums to 100%. */
  primary: VetoReasonRow[];
  /** Every reason exploded from veto_reasons — a multi-reason veto appears in
   *  more than one row, so these pct values can sum to more than 100%. */
  allReasons: VetoReasonRow[];
}

function classifyVetoReason(reason: string): VetoReasonCategory {
  if (reason.startsWith('UNKNOWN:')) return 'unknown';
  if (reason === 'LOW_SCORE') return 'low_score';
  if (reason === 'NONE' || reason === '') return 'none';
  return 'hard_fail';
}

/**
 * Aggregates why candidates were vetoed. `primary` groups by the first veto
 * reason (the single dominant cause); `allReasons` explodes the full
 * `veto_reasons` array so overlapping causes are visible. Splitting
 * `UNKNOWN:*` (enrichment reliability — recoverable) from hard `Hx` fails
 * (structural rejection) and `LOW_SCORE` is the point: it tells us whether the
 * funnel is narrow because of missing data or because of real risk signals.
 */
export function getVetoReasonBreakdown(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): VetoReasonBreakdown {
  const mod = rangeToModifier(opts.range);

  const totalStmt = db.prepare(
    `SELECT COUNT(*) AS n FROM candidates
     WHERE verdict = 'veto'${mod ? ` AND julianday(created_at) >= julianday('now', ?)` : ''}`,
  );
  const totalVetoed = ((mod ? totalStmt.get(mod) : totalStmt.get()) as { n: number }).n;

  const primaryStmt = db.prepare(
    `SELECT COALESCE(primary_veto_code, 'NONE') AS reason, COUNT(*) AS count
     FROM candidates
     WHERE verdict = 'veto'${mod ? ` AND julianday(created_at) >= julianday('now', ?)` : ''}
     GROUP BY reason
     ORDER BY count DESC`,
  );
  const allStmt = db.prepare(
    `SELECT je.value AS reason, COUNT(*) AS count
     FROM candidates c, json_each(c.veto_reasons) je
     WHERE c.verdict = 'veto'${mod ? ` AND julianday(c.created_at) >= julianday('now', ?)` : ''}
     GROUP BY je.value
     ORDER BY count DESC`,
  );

  const toRows = (rows: Array<{ reason: string; count: number }>): VetoReasonRow[] =>
    rows.map((r) => ({
      reason: r.reason,
      category: classifyVetoReason(r.reason),
      count: r.count,
      pct: totalVetoed > 0 ? (r.count / totalVetoed) * 100 : 0,
    }));

  const primaryRows = (mod ? primaryStmt.all(mod) : primaryStmt.all()) as Array<{
    reason: string;
    count: number;
  }>;
  const allRows = (mod ? allStmt.all(mod) : allStmt.all()) as Array<{
    reason: string;
    count: number;
  }>;

  return {
    totalVetoed,
    primary: toRows(primaryRows),
    allReasons: toRows(allRows),
  };
}

export interface ShadowVetoQualityRow {
  primaryVetoCode: string;
  trackedN: number;
  exitSimN: number;
  legacyPeakN: number;
  /** Backward-compatible alias for exitSimN. */
  n: number;
  avgPeakMfePct: number | null;
  avgMaxMaePct: number | null;
  hit25Pct: number | null;
  hit50Pct: number | null;
  /** Fee-adjusted dry-run net PnL averages (null when no exit metrics yet). */
  avgNetPnlSol: number | null;
  winRatePct: number | null;
  expectancySol: number | null;
  avgHoldMs: number | null;
}

/** Live accepted closed-trade stats vs counterfactual veto dry-run by reason. */
export interface VetoDryRunComparison {
  range: string;
  liveAccepted: {
    n: number;
    winRatePct: number;
    expectancySol: number;
    netPnlSol: number;
    avgMfePct: number | null;
    avgMaePct: number | null;
    avgHoldMs: number | null;
  };
  /** Dry-run outcomes for vetoed candidates, grouped by primary veto reason. */
  byVetoReason: VetoDryRunGroup[];
  /** Same completed outcomes expanded across every veto code. */
  byVetoCode: VetoDryRunGroup[];
  /** Exact veto-code combinations, preserving interactions. */
  byVetoCombination: VetoDryRunGroup[];
  trackedN: number;
  exitSimN: number;
  legacyPeakN: number;
  /** Backward-compatible alias for exitSimN. */
  vetoDryRunTotal: number;
}

export interface VetoDryRunGroup {
  primaryVetoCode: string;
  trackedN: number;
  exitSimN: number;
  legacyPeakN: number;
  n: number;
  winRatePct: number;
  expectancySol: number;
  netPnlSol: number;
  avgNetPnlSol: number;
  avgPeakMfePct: number | null;
  avgMaxMaePct: number | null;
  avgHoldMs: number | null;
  hit25Pct: number | null;
  hit50Pct: number | null;
}

export interface RelaxedRiskGroup {
  kind: 'clean' | 'relaxed';
  accepted: number;
  entered: number;
  closed: number;
  winRatePct: number;
  netPnlSol: number;
  emergencyExits: number;
  avgMfePct: number;
  avgMaePct: number;
}

export interface H4ReasonCount {
  reason: string;
  count: number;
}

export interface RelaxedRiskAnalytics {
  groups: RelaxedRiskGroup[];
  h4Reasons: H4ReasonCount[];
  rollback: {
    relaxedNetPnlSol: number;
    relaxedEmergencyExits: number;
    relaxedStopLosses: number;
    relaxedFailedEntries: number;
  };
}

export interface ShadowCoverage {
  eligible: number;
  started: number;
  skippedMissingPricing: number;
  droppedCapacity: number;
}

export function getShadowCoverage(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): ShadowCoverage {
  const mod = rangeToModifier(opts.range);
  const sql = `SELECT kind, COUNT(*) AS n FROM shadow_coverage_events
    ${mod ? `WHERE julianday(created_at) >= julianday('now', ?)` : ''}
    GROUP BY kind`;
  const rows = (mod ? db.prepare(sql).all(mod) : db.prepare(sql).all()) as Array<{ kind: string; n: number }>;
  const counts = new Map(rows.map((r) => [r.kind, r.n]));
  return {
    eligible: counts.get('eligible') ?? 0,
    started: counts.get('started') ?? 0,
    skippedMissingPricing: counts.get('skipped_missing_pricing') ?? 0,
    droppedCapacity: counts.get('dropped_capacity') ?? 0,
  };
}

/**
 * Veto quality from shadow (counterfactual) outcomes: for each primary veto
 * reason, how often did the token we rejected pump anyway, and what would the
 * exit-policy dry-run have realized (net PnL / win rate)? A high `hit50Pct` or
 * positive expectancy on a tunable threshold check (H5/H6/H7) or `LOW_SCORE` is
 * the evidence needed to justify loosening it. Rows with tiny `n` are not actionable.
 */
export function getShadowVetoQuality(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): { totalTracked: number; exitSimN: number; legacyPeakN: number; byReason: ShadowVetoQualityRow[] } {
  const mod = rangeToModifier(opts.range);
  const timeClause = mod ? `WHERE julianday(created_at) >= julianday('now', ?)` : '';

  const totalStmt = db.prepare(
    `SELECT COUNT(*) AS trackedN,
            COALESCE(SUM(CASE WHEN outcome_version LIKE 'exit_fsm_v%' AND net_pnl_sol IS NOT NULL THEN 1 ELSE 0 END), 0) AS exitSimN,
            COALESCE(SUM(CASE WHEN outcome_version IS NULL THEN 1 ELSE 0 END), 0) AS legacyPeakN
     FROM shadow_outcomes ${timeClause}`,
  );
  const totals = (mod ? totalStmt.get(mod) : totalStmt.get()) as {
    trackedN: number; exitSimN: number; legacyPeakN: number;
  };

  const stmt = db.prepare(
    `SELECT COALESCE(primary_veto_code, 'NONE') AS primaryVetoCode,
            COUNT(*) AS trackedN,
            SUM(CASE WHEN outcome_version LIKE 'exit_fsm_v%' AND net_pnl_sol IS NOT NULL THEN 1 ELSE 0 END) AS exitSimN,
            SUM(CASE WHEN outcome_version IS NULL THEN 1 ELSE 0 END) AS legacyPeakN,
            AVG(CASE WHEN outcome_version LIKE 'exit_fsm_v%' THEN peak_mfe_pct END) AS avgPeakMfePct,
            AVG(CASE WHEN outcome_version LIKE 'exit_fsm_v%' THEN max_mae_pct END) AS avgMaxMaePct,
            AVG(CASE WHEN outcome_version LIKE 'exit_fsm_v%' THEN hit_25 END) * 100 AS hit25Pct,
            AVG(CASE WHEN outcome_version LIKE 'exit_fsm_v%' THEN hit_50 END) * 100 AS hit50Pct,
            AVG(CASE WHEN outcome_version LIKE 'exit_fsm_v%' THEN net_pnl_sol END) AS avgNetPnlSol,
            AVG(CASE WHEN outcome_version LIKE 'exit_fsm_v%' AND net_pnl_sol > 0 THEN 1.0
                     WHEN outcome_version LIKE 'exit_fsm_v%' AND net_pnl_sol IS NOT NULL THEN 0.0 END) * 100 AS winRatePct,
            AVG(CASE WHEN outcome_version LIKE 'exit_fsm_v%' THEN net_pnl_sol END) AS expectancySol,
            AVG(CASE WHEN outcome_version LIKE 'exit_fsm_v%' THEN hold_ms END) AS avgHoldMs
     FROM shadow_outcomes
     ${timeClause}
     GROUP BY primaryVetoCode
     ORDER BY trackedN DESC`,
  );
  const rows = (mod ? stmt.all(mod) : stmt.all()) as Array<{
    primaryVetoCode: string;
    trackedN: number; exitSimN: number; legacyPeakN: number;
    avgPeakMfePct: number | null; avgMaxMaePct: number | null;
    hit25Pct: number | null; hit50Pct: number | null;
    avgNetPnlSol: number | null;
    winRatePct: number | null;
    expectancySol: number | null;
    avgHoldMs: number | null;
  }>;
  return {
    totalTracked: totals.trackedN,
    exitSimN: totals.exitSimN,
    legacyPeakN: totals.legacyPeakN,
    byReason: rows.map((r) => ({
      ...r,
      n: r.exitSimN,
      avgNetPnlSol: r.avgNetPnlSol ?? null,
      winRatePct: r.winRatePct ?? null,
      expectancySol: r.expectancySol ?? null,
      avgHoldMs: r.avgHoldMs ?? null,
    })),
  };
}

/**
 * Side-by-side comparison: live accepted closed performance vs veto dry-run
 * performance grouped by primary veto reason, for the same time range.
 * Live PnL is never mixed with counterfactual veto dry-run PnL (shadow_outcomes
 * is a separate table; this query never unions them into one PnL series).
 */
export function getVetoDryRunComparison(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all'; mode?: string | null } = {},
): VetoDryRunComparison {
  const range = opts.range ?? 'all';
  const mod = rangeToModifier(range);
  const mode = opts.mode ?? null;

  const liveFilters: string[] = [`state = 'CLOSED'`];
  const liveParams: string[] = [];
  if (mod) {
    liveFilters.push(`julianday(COALESCE(closed_at, created_at)) >= julianday('now', ?)`);
    liveParams.push(mod);
  }
  if (mode) {
    liveFilters.push(`mode = ?`);
    liveParams.push(mode);
  }
  const liveWhere = liveFilters.join(' AND ');
  const liveSql = `
    WITH latest AS (${latestPositionsSql()})
    SELECT COALESCE(net_pnl_sol, pnl_sol, 0) AS netPnl,
           mfe_pct AS mfePct,
           mae_pct AS maePct,
           hold_ms AS holdMs
    FROM latest
    WHERE ${liveWhere}
    ORDER BY julianday(COALESCE(closed_at, created_at)) ASC, id ASC`;
  const liveRows = (
    liveParams.length ? db.prepare(liveSql).all(...liveParams) : db.prepare(liveSql).all()
  ) as Array<{ netPnl: number; mfePct: number | null; maePct: number | null; holdMs: number | null }>;

  const livePnls = liveRows.map((r) => r.netPnl);
  const liveStats = computePerformanceStats(livePnls);
  const liveMfe = liveRows.map((r) => r.mfePct).filter((x): x is number => x != null);
  const liveMae = liveRows.map((r) => r.maePct).filter((x): x is number => x != null);
  const liveHold = liveRows.map((r) => r.holdMs).filter((x): x is number => x != null);

  const totalSql = `SELECT COUNT(*) AS trackedN,
      COALESCE(SUM(CASE WHEN outcome_version LIKE 'exit_fsm_v%' AND net_pnl_sol IS NOT NULL THEN 1 ELSE 0 END), 0) AS exitSimN,
      COALESCE(SUM(CASE WHEN outcome_version IS NULL THEN 1 ELSE 0 END), 0) AS legacyPeakN
    FROM shadow_outcomes ${mod ? `WHERE julianday(created_at) >= julianday('now', ?)` : ''}`;
  const totals = (mod ? db.prepare(totalSql).get(mod) : db.prepare(totalSql).get()) as {
    trackedN: number; exitSimN: number; legacyPeakN: number;
  };

  type RawGroup = {
    primaryVetoCode: string;
    trackedN: number; exitSimN: number; legacyPeakN: number;
    winRatePct: number | null;
    avgNetPnlSol: number | null;
    netPnlSol: number;
    avgPeakMfePct: number | null;
    avgMaxMaePct: number | null;
    avgHoldMs: number | null;
    hit25Pct: number | null;
    hit50Pct: number | null;
  };
  const aggregateSelect = (label: string) => `SELECT ${label} AS primaryVetoCode,
      COUNT(*) AS trackedN,
      SUM(CASE WHEN outcome_version LIKE 'exit_fsm_v%' AND net_pnl_sol IS NOT NULL THEN 1 ELSE 0 END) AS exitSimN,
      SUM(CASE WHEN outcome_version IS NULL THEN 1 ELSE 0 END) AS legacyPeakN,
      AVG(CASE WHEN outcome_version LIKE 'exit_fsm_v%' AND net_pnl_sol > 0 THEN 1.0
               WHEN outcome_version LIKE 'exit_fsm_v%' AND net_pnl_sol IS NOT NULL THEN 0.0 END) * 100 AS winRatePct,
      AVG(CASE WHEN outcome_version LIKE 'exit_fsm_v%' THEN net_pnl_sol END) AS avgNetPnlSol,
      SUM(CASE WHEN outcome_version LIKE 'exit_fsm_v%' THEN COALESCE(net_pnl_sol, 0) ELSE 0 END) AS netPnlSol,
      AVG(CASE WHEN outcome_version LIKE 'exit_fsm_v%' THEN peak_mfe_pct END) AS avgPeakMfePct,
      AVG(CASE WHEN outcome_version LIKE 'exit_fsm_v%' THEN max_mae_pct END) AS avgMaxMaePct,
      AVG(CASE WHEN outcome_version LIKE 'exit_fsm_v%' THEN hold_ms END) AS avgHoldMs,
      AVG(CASE WHEN outcome_version LIKE 'exit_fsm_v%' THEN hit_25 END) * 100 AS hit25Pct,
      AVG(CASE WHEN outcome_version LIKE 'exit_fsm_v%' THEN hit_50 END) * 100 AS hit50Pct`;
  const timeWhere = mod ? `WHERE julianday(s.created_at) >= julianday('now', ?)` : '';
  const run = (sql: string): RawGroup[] => (mod ? db.prepare(sql).all(mod) : db.prepare(sql).all()) as RawGroup[];
  const reasonRows = run(`${aggregateSelect("COALESCE(primary_veto_code, 'NONE')")}
    FROM shadow_outcomes s ${timeWhere} GROUP BY primaryVetoCode ORDER BY trackedN DESC`);
  const codeRows = run(`${aggregateSelect("COALESCE(je.value, 'NONE')")}
    FROM shadow_outcomes s
    LEFT JOIN json_each(COALESCE(s.veto_codes_json, json_array(s.primary_veto_code))) je
    ${timeWhere} GROUP BY primaryVetoCode ORDER BY trackedN DESC`);
  const combinationRows = run(`${aggregateSelect("COALESCE(veto_codes_json, json_array(primary_veto_code), '[]')")}
    FROM shadow_outcomes s ${timeWhere} GROUP BY primaryVetoCode ORDER BY trackedN DESC`);
  const mapGroups = (rows: RawGroup[]): VetoDryRunGroup[] => rows.map((r) => ({
    primaryVetoCode: r.primaryVetoCode,
    trackedN: r.trackedN,
    exitSimN: r.exitSimN,
    legacyPeakN: r.legacyPeakN,
    n: r.exitSimN,
    winRatePct: r.winRatePct ?? 0,
    expectancySol: r.avgNetPnlSol ?? 0,
    netPnlSol: r.netPnlSol,
    avgNetPnlSol: r.avgNetPnlSol ?? 0,
    avgPeakMfePct: r.avgPeakMfePct,
    avgMaxMaePct: r.avgMaxMaePct,
    avgHoldMs: r.avgHoldMs,
    hit25Pct: r.hit25Pct,
    hit50Pct: r.hit50Pct,
  }));

  return {
    range,
    liveAccepted: {
      n: liveStats.tradeCount,
      winRatePct: liveStats.winRatePct,
      expectancySol: liveStats.expectancySol,
      netPnlSol: liveStats.realizedPnlSol,
      avgMfePct: liveMfe.length ? liveMfe.reduce((a, b) => a + b, 0) / liveMfe.length : null,
      avgMaePct: liveMae.length ? liveMae.reduce((a, b) => a + b, 0) / liveMae.length : null,
      avgHoldMs: liveHold.length ? liveHold.reduce((a, b) => a + b, 0) / liveHold.length : null,
    },
    byVetoReason: mapGroups(reasonRows),
    byVetoCode: mapGroups(codeRows),
    byVetoCombination: mapGroups(combinationRows),
    trackedN: totals.trackedN,
    exitSimN: totals.exitSimN,
    legacyPeakN: totals.legacyPeakN,
    vetoDryRunTotal: totals.exitSimN,
  };
}

// ---------------------------------------------------------------------------
// Execution drag: live vs the dry-run twin
// ---------------------------------------------------------------------------

/**
 * Which legs of a mint are present and terminal.
 *
 *   both      — live traded it and the twin closed: full delta
 *   dry_only  — live never entered (blocked / failed): opportunity cost
 *   live_only — a live close with no twin: a COVERAGE GAP, surfaced not hidden
 */
export type DragCohort = 'both' | 'dry_only' | 'live_only';

export interface ExecutionDragRow {
  mint: string;
  cohort: DragCohort;
  liveStatus: string | null;
  liveNetPnlSol: number | null;
  dryNetPnlSol: number | null;
  netPnlDeltaSol: number | null;
  netPnlDeltaSolSizeAdj: number | null;
  pnlPctDelta: number | null;
  entryDragPct: number | null;
  exitDragPct: number | null;
  feeDeltaSol: number | null;
  fillCountDelta: number | null;
  holdMsDelta: number | null;
  exitConfirmMs: number | null;
  mfeDeltaPct: number | null;
  liveExitReason: string | null;
  dryExitReason: string | null;
  exitTriggerMatch: boolean | null;
  liveSizeSol: number | null;
  drySizeSol: number | null;
  at: string | null;
}

export interface ExecutionDragComparison {
  range: '24h' | '7d' | '30d' | 'all';
  sessionScoped: boolean;
  cohorts: { bothN: number; dryOnlyN: number; liveOnlyN: number; incompleteN: number; unknownStatusN: number };
  totals: {
    netPnlDeltaSol: number;
    netPnlDeltaSolSizeAdj: number;
    feeDeltaSol: number;
    opportunityCostSol: number;
    liveNetPnlSol: number;
    dryNetPnlSol: number;
  };
  perTrade: {
    avgNetPnlDeltaSol: number | null;
    medianNetPnlDeltaSol: number | null;
    avgEntryDragPct: number | null;
    avgExitDragPct: number | null;
    medianHoldMsDelta: number | null;
    avgExitConfirmMs: number | null;
    exitTriggerMatchPct: number | null;
  };
  winRate: { livePct: number | null; dryPct: number | null; n: number };
  byLiveStatus: Array<{
    liveStatus: string;
    n: number;
    dryNetPnlSol: number;
    liveNetPnlSol: number;
    netPnlDeltaSol: number;
  }>;
  byDryExitReason: Array<{
    exitReason: string;
    n: number;
    netPnlDeltaSol: number;
    medianHoldMsDelta: number | null;
  }>;
  /**
   * Fees are `estimatePaperFees` on BOTH legs (the live manager estimates them
   * too), so feeDeltaSol only reflects differing fill counts — not observed
   * on-chain fees. The TOTAL delta is still correct, because live entry/exit
   * prices are real; only the fee/slippage decomposition is approximate.
   */
  caveats: string[];
  rows: ExecutionDragRow[];
}

interface DragJoinRow {
  mint: string;
  liveSizeSol: number | null;
  liveEntryPrice: number | null;
  liveExitPrice: number | null;
  liveExitReason: string | null;
  liveState: string | null;
  liveNetPnlSol: number | null;
  livePnlPct: number | null;
  liveFeesSol: number | null;
  liveMfePct: number | null;
  liveHoldMs: number | null;
  liveExitConfirmMs: number | null;
  liveAt: string | null;
  drySizeSol: number | null;
  dryEntryPrice: number | null;
  dryExitPrice: number | null;
  dryExitReason: string | null;
  dryLiveStatus: string | null;
  dryNetPnlSol: number | null;
  dryPnlPct: number | null;
  dryFeesSol: number | null;
  dryMfePct: number | null;
  dryHoldMs: number | null;
  dryFillCount: number | null;
  dryAt: string | null;
  liveFillCount: number;
}

/**
 * Per-mint live-vs-dry execution drag.
 *
 * Deltas and aggregates are computed in TypeScript rather than SQL — same
 * approach as getVetoDryRunComparison — because null handling across a
 * three-way outer join is far easier to read and unit-test here.
 *
 * Scoped to the CURRENT run session by default: without that, the `live_only`
 * cohort fills with trades that predate the twin and permanently biases every
 * aggregate. Pass `allSessions` to opt out.
 */
export function getExecutionDragComparison(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all'; limit?: number; allSessions?: boolean; sessionId?: number | null } = {},
): ExecutionDragComparison {
  const range = opts.range ?? '7d';
  const mod = rangeToModifier(range);
  const sessionScoped = !opts.allSessions && opts.sessionId != null;

  const params: Array<string | number> = [];
  const timeWhere = (col: string) => {
    if (!mod) return '';
    params.push(mod);
    return ` AND julianday(${col}) >= julianday('now', ?)`;
  };
  const sessionWhere = () => {
    if (!sessionScoped) return '';
    params.push(opts.sessionId as number);
    return ` AND session_id = ?`;
  };

  // Params bind in SQL text order: live time, live session, dry time, dry session.
  const liveTime = timeWhere('COALESCE(closed_at, created_at)');
  const liveSession = sessionWhere();
  const dryTime = timeWhere('COALESCE(closed_at, created_at)');
  const drySession = sessionWhere();

  const sql = `
    WITH live AS (
      SELECT mint, size_sol, entry_price, exit_price, exit_reason, state,
             COALESCE(net_pnl_sol, pnl_sol) AS net_pnl_sol, pnl_pct, fees_sol,
             mfe_pct, hold_ms, exit_trigger_to_confirm_ms,
             COALESCE(closed_at, created_at) AS at
      FROM positions
      WHERE rowid IN (SELECT MAX(rowid) FROM positions GROUP BY mint)
        AND state IN ('CLOSED','FAILED')${liveTime}${liveSession}
    ),
    dry AS (
      SELECT mint, size_sol, entry_price, exit_price, exit_reason, live_status,
             net_pnl_sol, pnl_pct, fees_sol, mfe_pct, hold_ms, fill_count,
             COALESCE(closed_at, created_at) AS at
      FROM dry_run_positions
      WHERE rowid IN (SELECT MAX(rowid) FROM dry_run_positions GROUP BY mint)
        AND state = 'CLOSED'${dryTime}${drySession}
    ),
    mints AS (SELECT mint FROM live UNION SELECT mint FROM dry),
    fills AS (SELECT mint, COUNT(*) AS n FROM position_fills GROUP BY mint)
    SELECT m.mint AS mint,
           l.size_sol AS liveSizeSol, l.entry_price AS liveEntryPrice, l.exit_price AS liveExitPrice,
           l.exit_reason AS liveExitReason, l.state AS liveState, l.net_pnl_sol AS liveNetPnlSol,
           l.pnl_pct AS livePnlPct, l.fees_sol AS liveFeesSol, l.mfe_pct AS liveMfePct,
           l.hold_ms AS liveHoldMs, l.exit_trigger_to_confirm_ms AS liveExitConfirmMs, l.at AS liveAt,
           d.size_sol AS drySizeSol, d.entry_price AS dryEntryPrice, d.exit_price AS dryExitPrice,
           d.exit_reason AS dryExitReason, d.live_status AS dryLiveStatus, d.net_pnl_sol AS dryNetPnlSol,
           d.pnl_pct AS dryPnlPct, d.fees_sol AS dryFeesSol, d.mfe_pct AS dryMfePct,
           d.hold_ms AS dryHoldMs, d.fill_count AS dryFillCount, d.at AS dryAt,
           COALESCE(f.n, 0) AS liveFillCount
    FROM mints m
    LEFT JOIN live l ON l.mint = m.mint
    LEFT JOIN dry d ON d.mint = m.mint
    LEFT JOIN fills f ON f.mint = m.mint
    ORDER BY julianday(COALESCE(l.at, d.at)) ASC`;

  const joined = (params.length ? db.prepare(sql).all(...params) : db.prepare(sql).all()) as unknown as DragJoinRow[];

  // Mints still in flight on either leg are excluded from aggregates but counted,
  // so a small `both` sample is never mistaken for a complete one.
  const incompleteN = countIncomplete(db, sessionScoped ? (opts.sessionId as number) : null);

  const rows: ExecutionDragRow[] = joined.map((r) => {
    const hasLive = r.liveNetPnlSol != null || r.liveState != null;
    const hasDry = r.dryNetPnlSol != null || r.dryLiveStatus != null;
    // A live FAILED row means capital never went in, so it belongs with the
    // blocked cohort, not with `both`.
    const liveEntered = hasLive && r.liveState === 'CLOSED';
    const cohort: DragCohort = liveEntered && hasDry ? 'both' : hasDry ? 'dry_only' : 'live_only';

    const liveNet = r.liveNetPnlSol ?? null;
    const dryNet = r.dryNetPnlSol ?? null;
    const bothClosed = cohort === 'both' && liveNet != null && dryNet != null;
    const sizeRatio =
      r.liveSizeSol && r.drySizeSol && r.drySizeSol > 0 ? r.liveSizeSol / r.drySizeSol : 1;

    return {
      mint: r.mint,
      cohort,
      liveStatus: r.dryLiveStatus ?? null,
      liveNetPnlSol: liveNet,
      dryNetPnlSol: dryNet,
      netPnlDeltaSol: bothClosed ? liveNet! - dryNet! : null,
      netPnlDeltaSolSizeAdj: bothClosed ? liveNet! - dryNet! * sizeRatio : null,
      pnlPctDelta: bothClosed && r.livePnlPct != null && r.dryPnlPct != null ? r.livePnlPct - r.dryPnlPct : null,
      entryDragPct: ratioPct(r.liveEntryPrice, r.dryEntryPrice, cohort === 'both'),
      exitDragPct: ratioPct(r.liveExitPrice, r.dryExitPrice, cohort === 'both'),
      feeDeltaSol: bothClosed && r.liveFeesSol != null && r.dryFeesSol != null ? r.liveFeesSol - r.dryFeesSol : null,
      fillCountDelta: cohort === 'both' ? r.liveFillCount - (r.dryFillCount ?? 0) : null,
      holdMsDelta: bothClosed && r.liveHoldMs != null && r.dryHoldMs != null ? r.liveHoldMs - r.dryHoldMs : null,
      exitConfirmMs: r.liveExitConfirmMs ?? null,
      mfeDeltaPct: bothClosed && r.liveMfePct != null && r.dryMfePct != null ? r.liveMfePct - r.dryMfePct : null,
      liveExitReason: r.liveExitReason ?? null,
      dryExitReason: r.dryExitReason ?? null,
      exitTriggerMatch:
        cohort === 'both' && r.liveExitReason != null && r.dryExitReason != null
          ? r.liveExitReason === r.dryExitReason
          : null,
      liveSizeSol: r.liveSizeSol ?? null,
      drySizeSol: r.drySizeSol ?? null,
      at: r.liveAt ?? r.dryAt ?? null,
    };
  });

  const both = rows.filter((r) => r.cohort === 'both');
  const dryOnly = rows.filter((r) => r.cohort === 'dry_only');
  const liveOnly = rows.filter((r) => r.cohort === 'live_only');
  const deltas = both.map((r) => r.netPnlDeltaSol).filter(isNum);
  const holdDeltas = both.map((r) => r.holdMsDelta).filter(isNum);
  const matches = both.map((r) => r.exitTriggerMatch).filter((x): x is boolean => x !== null);

  const byLiveStatus = groupSum(
    rows.filter((r) => r.liveStatus !== null),
    (r) => r.liveStatus as string,
  ).map(([liveStatus, group]) => ({
    liveStatus,
    n: group.length,
    dryNetPnlSol: sum(group.map((r) => r.dryNetPnlSol ?? 0)),
    liveNetPnlSol: sum(group.map((r) => r.liveNetPnlSol ?? 0)),
    netPnlDeltaSol: sum(group.map((r) => r.netPnlDeltaSol ?? 0)),
  }));

  const byDryExitReason = groupSum(both, (r) => r.dryExitReason ?? 'UNKNOWN').map(([exitReason, group]) => ({
    exitReason,
    n: group.length,
    netPnlDeltaSol: sum(group.map((r) => r.netPnlDeltaSol ?? 0)),
    medianHoldMsDelta: median(group.map((r) => r.holdMsDelta).filter(isNum)),
  }));

  return {
    range,
    sessionScoped,
    cohorts: {
      bothN: both.length,
      dryOnlyN: dryOnly.length,
      liveOnlyN: liveOnly.length,
      incompleteN,
      unknownStatusN: rows.filter((r) => r.liveStatus === 'unknown').length,
    },
    totals: {
      netPnlDeltaSol: sum(deltas),
      netPnlDeltaSolSizeAdj: sum(both.map((r) => r.netPnlDeltaSolSizeAdj).filter(isNum)),
      feeDeltaSol: sum(both.map((r) => r.feeDeltaSol).filter(isNum)),
      // What the blocked / failed cohort would have made had live traded it.
      opportunityCostSol: sum(dryOnly.map((r) => r.dryNetPnlSol ?? 0)),
      liveNetPnlSol: sum(both.map((r) => r.liveNetPnlSol ?? 0)),
      dryNetPnlSol: sum(both.map((r) => r.dryNetPnlSol ?? 0)),
    },
    perTrade: {
      avgNetPnlDeltaSol: avg(deltas),
      medianNetPnlDeltaSol: median(deltas),
      avgEntryDragPct: avg(both.map((r) => r.entryDragPct).filter(isNum)),
      avgExitDragPct: avg(both.map((r) => r.exitDragPct).filter(isNum)),
      medianHoldMsDelta: median(holdDeltas),
      avgExitConfirmMs: avg(both.map((r) => r.exitConfirmMs).filter(isNum)),
      exitTriggerMatchPct: matches.length ? (matches.filter(Boolean).length / matches.length) * 100 : null,
    },
    winRate: {
      livePct: both.length ? (both.filter((r) => (r.liveNetPnlSol ?? 0) > 0).length / both.length) * 100 : null,
      dryPct: both.length ? (both.filter((r) => (r.dryNetPnlSol ?? 0) > 0).length / both.length) * 100 : null,
      n: both.length,
    },
    byLiveStatus,
    byDryExitReason,
    caveats: [
      'Fees are estimated on BOTH legs (the live manager also uses estimatePaperFees), so feeDeltaSol reflects differing fill counts, not observed on-chain fees.',
      'netPnlDeltaSol is still the correct TOTAL drag: live entry/exit prices are real fills. Only the fee-vs-slippage decomposition is approximate.',
      'entryDragPct already includes the buy-side swap fee — do not add it to feeDeltaSol.',
      'exitDragPct uses the LAST fill price when a ladder exits partially; prefer pnlPctDelta as the headline exit measure.',
    ],
    rows: rows.slice(0, clampLimit(opts.limit, 200, 1000)),
  };
}

/** Mints where one leg is still in flight — excluded from aggregates, reported. */
function countIncomplete(db: DB, sessionId: number | null): number {
  const where = sessionId != null ? ` AND session_id = ?` : '';
  const sql = `
    SELECT (
      SELECT COUNT(*) FROM (${latestPositionsSql()}) WHERE state IN ('PENDING_ENTRY','OPEN','EXITING')${where}
    ) + (
      SELECT COUNT(*) FROM (${latestPositionsSql('dry_run_positions')}) WHERE state = 'OPEN'${where}
    ) AS n`;
  const row = (sessionId != null ? db.prepare(sql).get(sessionId, sessionId) : db.prepare(sql).get()) as { n: number };
  return row.n;
}

export function buildExecutionDragCsv(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all'; allSessions?: boolean; sessionId?: number | null } = {},
): string {
  const cmp = getExecutionDragComparison(db, { ...opts, limit: 1000 });
  const lines = [
    '# fees are estimated on both legs; feeDeltaSol reflects fill-count differences, not observed on-chain fees',
    [
      'mint', 'cohort', 'live_status', 'live_net_pnl_sol', 'dry_net_pnl_sol', 'net_pnl_delta_sol',
      'net_pnl_delta_sol_size_adj', 'pnl_pct_delta', 'entry_drag_pct', 'exit_drag_pct', 'fee_delta_sol',
      'fill_count_delta', 'hold_ms_delta', 'exit_confirm_ms', 'mfe_delta_pct', 'live_exit_reason',
      'dry_exit_reason', 'exit_trigger_match', 'live_size_sol', 'dry_size_sol', 'at',
    ].join(','),
  ];
  for (const r of cmp.rows) {
    lines.push(
      [
        r.mint, r.cohort, r.liveStatus ?? '', csvNum(r.liveNetPnlSol), csvNum(r.dryNetPnlSol),
        csvNum(r.netPnlDeltaSol), csvNum(r.netPnlDeltaSolSizeAdj), csvNum(r.pnlPctDelta),
        csvNum(r.entryDragPct), csvNum(r.exitDragPct), csvNum(r.feeDeltaSol), csvNum(r.fillCountDelta),
        csvNum(r.holdMsDelta), csvNum(r.exitConfirmMs), csvNum(r.mfeDeltaPct), r.liveExitReason ?? '',
        r.dryExitReason ?? '', r.exitTriggerMatch === null ? '' : String(r.exitTriggerMatch),
        csvNum(r.liveSizeSol), csvNum(r.drySizeSol), r.at ?? '',
      ].join(','),
    );
  }
  return lines.join('\n') + '\n';
}

function csvNum(v: number | null): string {
  return v === null || !Number.isFinite(v) ? '' : String(v);
}

function isNum(v: number | null): v is number {
  return v !== null && Number.isFinite(v);
}

function sum(values: number[]): number {
  return values.reduce((a, b) => a + b, 0);
}

function avg(values: number[]): number | null {
  return values.length ? sum(values) / values.length : null;
}

function median(values: number[]): number | null {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid]! : (sorted[mid - 1]! + sorted[mid]!) / 2;
}

/** Percentage change of `live` against `dry`, or null when either is unusable. */
function ratioPct(live: number | null, dry: number | null, applies: boolean): number | null {
  if (!applies || live == null || dry == null || !(dry > 0) || !(live > 0)) return null;
  return (live / dry - 1) * 100;
}

function groupSum<T>(rows: T[], key: (row: T) => string): Array<[string, T[]]> {
  const map = new Map<string, T[]>();
  for (const row of rows) {
    const k = key(row);
    const bucket = map.get(k);
    if (bucket) bucket.push(row);
    else map.set(k, [row]);
  }
  return [...map.entries()].sort((a, b) => b[1].length - a[1].length);
}

export function getRelaxedRiskAnalytics(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): RelaxedRiskAnalytics {
  const mod = rangeToModifier(opts.range);
  const candTime = mod ? `AND julianday(created_at) >= julianday('now', ?)` : '';
  const posTime = mod ? `AND julianday(created_at) >= julianday('now', ?)` : '';
  const closedTime = mod ? `AND julianday(COALESCE(closed_at, created_at)) >= julianday('now', ?)` : '';

  const acceptedRows = (
    mod
      ? db.prepare(
          `SELECT relaxed_risk AS relaxedRisk, COUNT(*) AS n
           FROM candidates
           WHERE verdict = 'accept' ${candTime}
           GROUP BY relaxed_risk`,
        ).all(mod)
      : db.prepare(
          `SELECT relaxed_risk AS relaxedRisk, COUNT(*) AS n
           FROM candidates
           WHERE verdict = 'accept'
           GROUP BY relaxed_risk`,
        ).all()
  ) as Array<{ relaxedRisk: number; n: number }>;

  const enteredRows = (
    mod
      ? db.prepare(
          `WITH latest AS (${latestPositionsSql()})
           SELECT relaxed_risk AS relaxedRisk, COUNT(*) AS n
           FROM latest
           WHERE state IN ('PENDING_ENTRY','OPEN','EXITING','CLOSED','FAILED') ${posTime}
           GROUP BY relaxed_risk`,
        ).all(mod)
      : db.prepare(
          `WITH latest AS (${latestPositionsSql()})
           SELECT relaxed_risk AS relaxedRisk, COUNT(*) AS n
           FROM latest
           WHERE state IN ('PENDING_ENTRY','OPEN','EXITING','CLOSED','FAILED')
           GROUP BY relaxed_risk`,
        ).all()
  ) as Array<{ relaxedRisk: number; n: number }>;

  const closedRows = (
    mod
      ? db.prepare(
          `SELECT relaxed_risk AS relaxedRisk,
                  COUNT(*) AS closed,
                  COALESCE(SUM(CASE WHEN COALESCE(net_pnl_sol, pnl_sol) > 0 THEN 1 ELSE 0 END), 0) AS wins,
                  COALESCE(SUM(COALESCE(net_pnl_sol, pnl_sol)), 0) AS netPnlSol,
                  COALESCE(SUM(CASE WHEN exit_reason = 'EMERGENCY_EXIT' THEN 1 ELSE 0 END), 0) AS emergencyExits,
                  COALESCE(AVG(mfe_pct), 0) AS avgMfePct,
                  COALESCE(AVG(mae_pct), 0) AS avgMaePct
           FROM positions
           WHERE state = 'CLOSED' ${closedTime}
           GROUP BY relaxed_risk`,
        ).all(mod)
      : db.prepare(
          `SELECT relaxed_risk AS relaxedRisk,
                  COUNT(*) AS closed,
                  COALESCE(SUM(CASE WHEN COALESCE(net_pnl_sol, pnl_sol) > 0 THEN 1 ELSE 0 END), 0) AS wins,
                  COALESCE(SUM(COALESCE(net_pnl_sol, pnl_sol)), 0) AS netPnlSol,
                  COALESCE(SUM(CASE WHEN exit_reason = 'EMERGENCY_EXIT' THEN 1 ELSE 0 END), 0) AS emergencyExits,
                  COALESCE(AVG(mfe_pct), 0) AS avgMfePct,
                  COALESCE(AVG(mae_pct), 0) AS avgMaePct
           FROM positions
           WHERE state = 'CLOSED'
           GROUP BY relaxed_risk`,
        ).all()
  ) as Array<{
    relaxedRisk: number;
    closed: number;
    wins: number;
    netPnlSol: number;
    emergencyExits: number;
    avgMfePct: number;
    avgMaePct: number;
  }>;

  const h4Rows = (
    mod
      ? db.prepare(
          `SELECT COALESCE(json_extract(je.value, '$.reason'), 'none') AS reason, COUNT(*) AS count
           FROM candidates c, json_each(c.hard_check_results) je
           WHERE json_extract(je.value, '$.id') = 'H4'
             AND julianday(c.created_at) >= julianday('now', ?)
           GROUP BY reason
           ORDER BY count DESC`,
        ).all(mod)
      : db.prepare(
          `SELECT COALESCE(json_extract(je.value, '$.reason'), 'none') AS reason, COUNT(*) AS count
           FROM candidates c, json_each(c.hard_check_results) je
           WHERE json_extract(je.value, '$.id') = 'H4'
           GROUP BY reason
           ORDER BY count DESC`,
        ).all()
  ) as unknown as H4ReasonCount[];

  const rollback = (
    mod
      ? db.prepare(
          `SELECT
             COALESCE(SUM(CASE WHEN state = 'CLOSED' THEN COALESCE(net_pnl_sol, pnl_sol) ELSE 0 END), 0) AS relaxedNetPnlSol,
             COALESCE(SUM(CASE WHEN state = 'CLOSED' AND exit_reason = 'EMERGENCY_EXIT' THEN 1 ELSE 0 END), 0) AS relaxedEmergencyExits,
             COALESCE(SUM(CASE WHEN state = 'CLOSED' AND exit_reason = 'STOP_LOSS' THEN 1 ELSE 0 END), 0) AS relaxedStopLosses,
             COALESCE(SUM(CASE WHEN state = 'FAILED' THEN 1 ELSE 0 END), 0) AS relaxedFailedEntries
           FROM positions
           WHERE relaxed_risk = 1 ${posTime}`,
        ).get(mod)
      : db.prepare(
          `SELECT
             COALESCE(SUM(CASE WHEN state = 'CLOSED' THEN COALESCE(net_pnl_sol, pnl_sol) ELSE 0 END), 0) AS relaxedNetPnlSol,
             COALESCE(SUM(CASE WHEN state = 'CLOSED' AND exit_reason = 'EMERGENCY_EXIT' THEN 1 ELSE 0 END), 0) AS relaxedEmergencyExits,
             COALESCE(SUM(CASE WHEN state = 'CLOSED' AND exit_reason = 'STOP_LOSS' THEN 1 ELSE 0 END), 0) AS relaxedStopLosses,
             COALESCE(SUM(CASE WHEN state = 'FAILED' THEN 1 ELSE 0 END), 0) AS relaxedFailedEntries
           FROM positions
           WHERE relaxed_risk = 1`,
        ).get()
  ) as RelaxedRiskAnalytics['rollback'];

  const accepted = byRelaxed(acceptedRows, 'n');
  const entered = byRelaxed(enteredRows, 'n');
  const closed = new Map(closedRows.map((r) => [Number(r.relaxedRisk) === 1 ? 'relaxed' : 'clean', r]));
  const groups: RelaxedRiskGroup[] = (['clean', 'relaxed'] as const).map((kind) => {
    const c = closed.get(kind);
    const closedCount = c?.closed ?? 0;
    return {
      kind,
      accepted: accepted.get(kind) ?? 0,
      entered: entered.get(kind) ?? 0,
      closed: closedCount,
      winRatePct: closedCount > 0 ? ((c?.wins ?? 0) / closedCount) * 100 : 0,
      netPnlSol: c?.netPnlSol ?? 0,
      emergencyExits: c?.emergencyExits ?? 0,
      avgMfePct: c?.avgMfePct ?? 0,
      avgMaePct: c?.avgMaePct ?? 0,
    };
  });

  return { groups, h4Reasons: h4Rows, rollback };
}

/** Mint-level veto dry-run outcomes for the operator table (shadow_outcomes only). */
export interface ShadowOutcomeRow {
  id: number;
  mint: string;
  verdict: string;
  primaryVetoCode: string | null;
  vetoCodes: string[];
  baselinePrice: number;
  peakPrice: number | null;
  troughPrice: number | null;
  peakMfePct: number | null;
  maxMaePct: number | null;
  hit25: boolean;
  hit50: boolean;
  netPnlSol: number | null;
  grossPnlSol: number | null;
  feesSol: number | null;
  pnlPct: number | null;
  exitReason: string | null;
  holdMs: number | null;
  sizeSol: number | null;
  samples: number;
  trackedMs: number | null;
  outcomeVersion: string | null;
  createdAt: string;
}

export function listShadowOutcomes(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all'; limit?: number } = {},
): ShadowOutcomeRow[] {
  const limit = clampLimit(opts.limit, 80, 500);
  const mod = rangeToModifier(opts.range);
  const sql = mod
    ? `SELECT rowid AS id, mint, verdict, primary_veto_code, veto_codes_json, baseline_price,
              peak_price, trough_price, peak_mfe_pct, max_mae_pct, hit_25, hit_50,
              net_pnl_sol, gross_pnl_sol, fees_sol, pnl_pct, exit_reason, hold_ms,
              size_sol, samples, tracked_ms, outcome_version, created_at
       FROM shadow_outcomes
       WHERE julianday(created_at) >= julianday('now', ?)
       ORDER BY julianday(created_at) DESC, rowid DESC
       LIMIT ?`
    : `SELECT rowid AS id, mint, verdict, primary_veto_code, veto_codes_json, baseline_price,
              peak_price, trough_price, peak_mfe_pct, max_mae_pct, hit_25, hit_50,
              net_pnl_sol, gross_pnl_sol, fees_sol, pnl_pct, exit_reason, hold_ms,
              size_sol, samples, tracked_ms, outcome_version, created_at
       FROM shadow_outcomes
       ORDER BY julianday(created_at) DESC, rowid DESC
       LIMIT ?`;
  const rows = (
    mod ? db.prepare(sql).all(mod, limit) : db.prepare(sql).all(limit)
  ) as Array<{
    id: number;
    mint: string;
    verdict: string;
    primary_veto_code: string | null;
    veto_codes_json: string | null;
    baseline_price: number;
    peak_price: number | null;
    trough_price: number | null;
    peak_mfe_pct: number | null;
    max_mae_pct: number | null;
    hit_25: number;
    hit_50: number;
    net_pnl_sol: number | null;
    gross_pnl_sol: number | null;
    fees_sol: number | null;
    pnl_pct: number | null;
    exit_reason: string | null;
    hold_ms: number | null;
    size_sol: number | null;
    samples: number;
    tracked_ms: number | null;
    outcome_version: string | null;
    created_at: string;
  }>;

  return rows.map((row) => ({
    id: row.id,
    mint: row.mint,
    verdict: row.verdict,
    primaryVetoCode: row.primary_veto_code,
    vetoCodes: parseJsonArray(row.veto_codes_json),
    baselinePrice: row.baseline_price,
    peakPrice: row.peak_price,
    troughPrice: row.trough_price,
    peakMfePct: row.peak_mfe_pct,
    maxMaePct: row.max_mae_pct,
    hit25: row.hit_25 === 1,
    hit50: row.hit_50 === 1,
    netPnlSol: row.net_pnl_sol,
    grossPnlSol: row.gross_pnl_sol,
    feesSol: row.fees_sol,
    pnlPct: row.pnl_pct,
    exitReason: row.exit_reason,
    holdMs: row.hold_ms,
    sizeSol: row.size_sol,
    samples: row.samples,
    trackedMs: row.tracked_ms,
    outcomeVersion: row.outcome_version,
    createdAt: row.created_at,
  }));
}

export function listPositions(
  db: DB,
  opts: { state?: 'open' | 'closed' | 'all'; limit?: number; track?: DataTrack; mode?: Config['mode'] } = {},
): DashboardPosition[] {
  const state = opts.state ?? 'all';
  const limit = clampLimit(opts.limit, 50, 200);
  const src = trackSource(opts.track, opts.mode);
  const table = src.table;
  const mf = sourceFilter(src);
  const activeList = ACTIVE_STATES.map((s) => `'${s}'`).join(',');
  let sql: string;

  if (state === 'closed') {
    sql = `SELECT rowid AS id, * FROM ${table} WHERE state = 'CLOSED'${mf} ORDER BY COALESCE(closed_at, created_at) DESC, rowid DESC LIMIT ?`;
  } else if (state === 'open') {
    sql = `WITH latest AS (${latestPositionsSql(table)}) SELECT * FROM latest WHERE state IN (${activeList})${mf} ORDER BY created_at DESC, id DESC LIMIT ?`;
  } else {
    sql = `WITH latest AS (${latestPositionsSql(table)}) SELECT * FROM latest WHERE 1 = 1${mf} ORDER BY created_at DESC, id DESC LIMIT ?`;
  }

  // Marks come from price_ticks, which only the LIVE poller writes. A dry row
  // has no mark of its own, and borrowing the live one would be a fabrication.
  const marks = src.isTwin ? new Map<string, number>() : latestMarks(db);
  return db.prepare(sql).all(limit).map((row) => mapPosition(row as Row, marks));
}

export function getPnlSeries(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d'; track?: Track; mode?: Config['mode'] } = {},
): PnlPoint[] {
  const modifier = opts.range === '30d' ? '-30 days' : opts.range === '24h' ? '-1 day' : '-7 days';
  if (opts.track === 'delta') return deltaPnlSeries(db, modifier);

  const src = trackSource(opts.track, opts.mode);
  const table = src.table;
  const mf = sourceFilter(src);
  const rows = db
    .prepare(
      `SELECT mint, COALESCE(net_pnl_sol, pnl_sol) AS pnl_sol, COALESCE(closed_at, created_at) AS time
       FROM ${table}
       WHERE state = 'CLOSED'${mf}
         AND COALESCE(closed_at, created_at) IS NOT NULL
         AND julianday(COALESCE(closed_at, created_at)) >= julianday('now', ?)
       ORDER BY julianday(COALESCE(closed_at, created_at)) ASC, rowid ASC`,
    )
    .all(modifier) as Array<{ mint: string; pnl_sol: number | null; time: string }>;

  let cumulative = 0;
  return rows.map((row) => {
    const pnl = row.pnl_sol ?? 0;
    cumulative += pnl;
    return { time: row.time, mint: row.mint, pnlSol: pnl, cumulativePnlSol: cumulative };
  });
}

/**
 * Cumulative live-minus-dry PnL over mints where BOTH legs closed. Ordered by
 * the live close so the curve reads as "drag accumulated over the week".
 */
function deltaPnlSeries(db: DB, modifier: string): PnlPoint[] {
  const rows = db
    .prepare(
      `WITH live AS (
         SELECT mint, COALESCE(net_pnl_sol, pnl_sol) AS pnl, COALESCE(closed_at, created_at) AS time
         FROM positions
         WHERE rowid IN (SELECT MAX(rowid) FROM positions GROUP BY mint)
           AND state = 'CLOSED'
           AND julianday(COALESCE(closed_at, created_at)) >= julianday('now', ?)
       ),
       dry AS (
         SELECT mint, net_pnl_sol AS pnl
         FROM dry_run_positions
         WHERE rowid IN (SELECT MAX(rowid) FROM dry_run_positions GROUP BY mint)
           AND state = 'CLOSED'
       )
       SELECT l.mint AS mint, l.time AS time, (COALESCE(l.pnl, 0) - COALESCE(d.pnl, 0)) AS pnl_sol
       FROM live l
       JOIN dry d ON d.mint = l.mint
       ORDER BY julianday(l.time) ASC`,
    )
    .all(modifier) as Array<{ mint: string; pnl_sol: number; time: string }>;

  let cumulative = 0;
  return rows.map((row) => {
    cumulative += row.pnl_sol;
    return { time: row.time, mint: row.mint, pnlSol: row.pnl_sol, cumulativePnlSol: cumulative };
  });
}

export function listCandidates(db: DB, opts: { limit?: number } = {}): DashboardCandidate[] {
  const limit = clampLimit(opts.limit, 40, 150);
  const rows = db
    .prepare(
      `SELECT rowid AS id, mint, verdict, soft_score, veto_reasons, high_volatility,
              relaxed_risk, relaxed_reasons_json, created_at, hard_check_results
       FROM candidates
       ORDER BY created_at DESC, rowid DESC
       LIMIT ?`,
    )
    .all(limit) as Array<{
      id: number;
      mint: string;
      verdict: string | null;
      soft_score: number | null;
      veto_reasons: string | null;
      high_volatility: number;
      relaxed_risk: number;
      relaxed_reasons_json: string | null;
      created_at: string;
      hard_check_results: string | null;
    }>;

  return rows.map((row) => {
    const hardChecks = parseHardChecks(row.hard_check_results);
    return {
      id: row.id,
      mint: row.mint,
      verdict: row.verdict,
      softScore: row.soft_score,
      vetoReasons: parseJsonArray(row.veto_reasons),
      highVolatility: row.high_volatility === 1,
      relaxedRisk: row.relaxed_risk === 1,
      relaxedReasons: parseJsonArray(row.relaxed_reasons_json),
      createdAt: row.created_at,
      ...(hardChecks ? { hardChecks } : {}),
    };
  });
}

export function listEvents(
  db: DB,
  opts: { limit?: number; level?: OperatorEventLevel; category?: string } = {},
): DashboardEvent[] {
  const limit = clampLimit(opts.limit, 80, 300);
  const clauses: string[] = [];
  const params: Array<string | number> = [];
  if (opts.level) {
    clauses.push('level = ?');
    params.push(opts.level);
  }
  if (opts.category) {
    clauses.push('category = ?');
    params.push(opts.category);
  }
  params.push(limit);
  const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
  const rows = db
    .prepare(
      `SELECT id, category, level, message, entity_mint, payload_json, created_at
       FROM operator_events
       ${where}
       ORDER BY created_at DESC, id DESC
       LIMIT ?`,
    )
    .all(...params) as Array<{
      id: number;
      category: string;
      level: OperatorEventLevel;
      message: string;
      entity_mint: string | null;
      payload_json: string | null;
      created_at: string;
    }>;
  return rows.map((row) => ({
    id: row.id,
    category: row.category,
    level: row.level,
    message: row.message,
    entityMint: row.entity_mint,
    payload: parseJson(row.payload_json),
    createdAt: row.created_at,
  }));
}

const BLOTTER_HEADERS = [
  'mint',
  'state',
  'size_sol',
  'entry_price',
  'exit_price',
  'exit_reason',
  'gross_pnl_sol',
  'fees_sol',
  'net_pnl_sol',
  'pnl_pct',
  'mfe_pct',
  'mae_pct',
  'hold_ms',
  'exit_trigger_to_confirm_ms',
  'entry_soft_score',
  'high_volatility',
  'feed_source',
  'venue',
  'slippage_sol',
  'mode',
  'relaxed_risk',
  'relaxed_reasons_json',
  'opened_at',
  'closed_at',
  'entry_tx',
  'exit_tx',
  // Export completeness (work plan 2026-09-25 P0.5 / F15): the features the
  // review had to reconstruct by hand. Export names on the left; see
  // BLOTTER_SOURCE for the ones whose DB column is named differently.
  'score_components_json',
  'early_flow_sol',
  'early_flow_rate',
  'pool_sol_at_entry',
  'mint_age_ms',
  'entry_detect_to_open_ms',
  'creator',
  'top10_pct',
  'creator_pct',
  'mcap_sol_at_entry',
  'fee_tier_bps',
  'sellability_status',
  'sellability_reason',
  'pool_move_pct',
  'population_ok',
  'momentum_window_ms',
  'size_multiplier',
  'simulated',
  'features_json',
  'model_version',
  'model_prob',
  'decision_prob',
] as const;

/** Export header -> DB column, where they differ. */
const BLOTTER_SOURCE: Record<string, string> = {
  early_flow_sol: 'early_flow_net_sol',
  entry_detect_to_open_ms: 'detect_to_open_ms',
  top10_pct: 'top10_share',
  creator_pct: 'creator_share',
};

/**
 * Columns the live table has and the twin does not. Selected as NULL on the dry
 * track so both tracks emit identical headers — a diff of the two CSVs then
 * lines up column-for-column.
 */
const LIVE_ONLY_BLOTTER_COLUMNS = [
  'relaxed_reasons_json',
  'entry_tx',
  'exit_tx',
  'score_components_json',
  'early_flow_sol',
  'early_flow_rate',
  'pool_sol_at_entry',
  'top10_pct',
  'creator_pct',
  'momentum_window_ms',
  'size_multiplier',
  'features_json',
  'model_version',
  'model_prob',
  'decision_prob',
];

export function buildTradeBlotterCsv(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all'; track?: DataTrack; mode?: Config['mode'] } = {},
): string {
  const mod = rangeToModifier(opts.range);
  const src = trackSource(opts.track, opts.mode);
  const table = src.table;
  const isDry = src.isTwin;
  const blotterMf = sourceFilter(src);

  const select = BLOTTER_HEADERS.map((h) => {
    if (isDry && LIVE_ONLY_BLOTTER_COLUMNS.includes(h)) return `NULL AS ${h}`;
    if (h === 'gross_pnl_sol') return `COALESCE(gross_pnl_sol, pnl_sol) AS gross_pnl_sol`;
    if (h === 'net_pnl_sol') return `COALESCE(net_pnl_sol, pnl_sol) AS net_pnl_sol`;
    // A shadow decision can land after the position rows were written.
    if (h === 'decision_prob') {
      return `COALESCE(decision_prob, (SELECT c.decision_prob FROM candidates c WHERE c.mint = ${table}.mint ORDER BY c.rowid DESC LIMIT 1)) AS decision_prob`;
    }
    const src = BLOTTER_SOURCE[h];
    return src ? `${src} AS ${h}` : h;
  })
    // The twin's whole point is which candidates live did or didn't trade.
    .concat(isDry ? ['live_status'] : [])
    .join(', ');

  const where = mod
    ? `WHERE state = 'CLOSED'${blotterMf} AND julianday(COALESCE(closed_at, created_at)) >= julianday('now', ?)`
    : `WHERE state = 'CLOSED'${blotterMf}`;
  const sql = `SELECT ${select} FROM ${table} ${where}
     ORDER BY julianday(COALESCE(closed_at, created_at)) ASC, rowid ASC`;

  const rows = (mod ? db.prepare(sql).all(mod) : db.prepare(sql).all()) as Array<Record<string, unknown>>;
  const headers = [...BLOTTER_HEADERS, ...(isDry ? ['live_status'] : [])];
  const lines = [headers.join(',')];
  for (const row of rows) {
    lines.push(headers.map((h) => csvEscape(row[h])).join(','));
  }
  return lines.join('\n') + '\n';
}

/**
 * Detail blotter of capital-free veto dry-runs (`shadow_outcomes` only).
 * Never includes live positions — counterfactual performance only.
 */
export function buildVetoDryRunCsv(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): string {
  const mod = rangeToModifier(opts.range);
  const sql = `WITH latest_candidates AS (
      SELECT c.* FROM candidates c
      WHERE c.rowid = (SELECT MAX(c2.rowid) FROM candidates c2 WHERE c2.mint = c.mint)
    )
    SELECT s.mint, s.verdict, s.primary_veto_code, s.veto_codes_json,
           COALESCE(s.outcome_version,
             CASE WHEN s.net_pnl_sol IS NULL THEN 'legacy_peak_only' ELSE 'exit_fsm_v1' END) AS outcome_version,
           CASE WHEN s.outcome_version LIKE 'exit_fsm_v%' AND s.net_pnl_sol IS NOT NULL THEN 1 ELSE 0 END AS completed_exit_sim,
           s.baseline_price, s.peak_price, s.trough_price, s.peak_mfe_pct, s.max_mae_pct,
           s.hit_25, s.hit_50, s.net_pnl_sol, s.gross_pnl_sol, s.fees_sol, s.pnl_pct,
           s.exit_reason, s.hold_ms, s.size_sol, s.samples, s.tracked_ms,
           c.soft_score, c.top10_share, c.max_holder_share, c.creator_share,
           c.pool_sol_at_entry, c.buy_impact_pct, c.early_flow_net_sol, c.early_flow_rate,
           c.sellability_reason, c.sellability_tx_bytes, c.sellability_used_lookup_table,
           c.hard_check_results, s.created_at
    FROM shadow_outcomes s
    LEFT JOIN latest_candidates c ON c.mint = s.mint
    ${mod ? `WHERE julianday(s.created_at) >= julianday('now', ?)` : ''}
    ORDER BY julianday(s.created_at) ASC, s.rowid ASC`;
  const rows = (mod ? db.prepare(sql).all(mod) : db.prepare(sql).all()) as Array<Record<string, unknown>>;
  const headers = [
    'mint',
    'verdict',
    'primary_veto_code',
    'veto_codes_json',
    'outcome_version',
    'completed_exit_sim',
    'baseline_price',
    'peak_price',
    'trough_price',
    'peak_mfe_pct',
    'max_mae_pct',
    'hit_25',
    'hit_50',
    'net_pnl_sol',
    'gross_pnl_sol',
    'fees_sol',
    'pnl_pct',
    'exit_reason',
    'hold_ms',
    'size_sol',
    'samples',
    'tracked_ms',
    'soft_score',
    'top10_share',
    'max_holder_share',
    'creator_share',
    'pool_sol_at_entry',
    'buy_impact_pct',
    'early_flow_net_sol',
    'early_flow_rate',
    'sellability_reason',
    'sellability_tx_bytes',
    'sellability_used_lookup_table',
    'hard_check_results',
    'created_at',
  ];
  const lines = [headers.join(',')];
  for (const row of rows) {
    lines.push(headers.map((h) => csvEscape(row[h])).join(','));
  }
  return lines.join('\n') + '\n';
}

/**
 * Operator snapshot CSV: live accepted headline + per-veto-reason dry-run rows.
 * Aggregation is shipped `getVetoDryRunComparison` (not reimplemented here).
 */
export function buildVetoDryRunSummaryCsv(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all'; mode?: string | null } = {},
): string {
  const range = opts.range ?? 'all';
  const cmp = getVetoDryRunComparison(db, {
    range,
    ...(opts.mode !== undefined ? { mode: opts.mode } : {}),
  });
  const lines: string[] = [];
  lines.push('# Veto dry-run performance snapshot (counterfactual; not live capital)');
  lines.push(`# range=${cmp.range}`);
  lines.push(
    `# live_accepted n=${cmp.liveAccepted.n} win_pct=${cmp.liveAccepted.winRatePct.toFixed(2)} ` +
      `expectancy_sol=${cmp.liveAccepted.expectancySol} net_pnl_sol=${cmp.liveAccepted.netPnlSol} ` +
      `avg_mfe_pct=${cmp.liveAccepted.avgMfePct ?? ''} avg_mae_pct=${cmp.liveAccepted.avgMaePct ?? ''} ` +
      `avg_hold_ms=${cmp.liveAccepted.avgHoldMs ?? ''}`,
  );
  lines.push(`# tracked_n=${cmp.trackedN} exit_sim_n=${cmp.exitSimN} legacy_peak_n=${cmp.legacyPeakN}`);
  const headers = [
    'cohort_type',
    'primary_veto_code',
    'tracked_n',
    'exit_sim_n',
    'legacy_peak_n',
    'n',
    'win_rate_pct',
    'expectancy_sol',
    'net_pnl_sol',
    'avg_net_pnl_sol',
    'avg_peak_mfe_pct',
    'avg_max_mae_pct',
    'hit_25_pct',
    'hit_50_pct',
    'avg_hold_ms',
  ];
  lines.push(headers.join(','));
  const cohorts: Array<{ type: string; rows: VetoDryRunGroup[] }> = [
    { type: 'primary', rows: cmp.byVetoReason },
    { type: 'all_code', rows: cmp.byVetoCode },
    { type: 'combination', rows: cmp.byVetoCombination },
  ];
  for (const cohort of cohorts) for (const r of cohort.rows) {
    const row: Record<string, unknown> = {
      cohort_type: cohort.type,
      primary_veto_code: r.primaryVetoCode,
      tracked_n: r.trackedN,
      exit_sim_n: r.exitSimN,
      legacy_peak_n: r.legacyPeakN,
      n: r.n,
      win_rate_pct: r.winRatePct,
      expectancy_sol: r.expectancySol,
      net_pnl_sol: r.netPnlSol,
      avg_net_pnl_sol: r.avgNetPnlSol,
      avg_peak_mfe_pct: r.avgPeakMfePct,
      avg_max_mae_pct: r.avgMaxMaePct,
      hit_25_pct: r.hit25Pct,
      hit_50_pct: r.hit50Pct,
      avg_hold_ms: r.avgHoldMs,
    };
    lines.push(headers.map((h) => csvEscape(row[h])).join(','));
  }
  return lines.join('\n') + '\n';
}

export function buildOpsReport(
  db: DB,
  config: Config,
  risk: RiskSnapshot | null,
): Record<string, unknown> {
  const summary = getDashboardSummary(db, config);
  return {
    generatedAt: new Date().toISOString(),
    mode: config.mode,
    risk,
    summary,
    breakers: listBreakers(db, { limit: 20 }),
    funnel: getFunnelAnalytics(db, { range: '24h' }),
    relaxedRisk: getRelaxedRiskAnalytics(db, { range: '24h' }),
  };
}

export function buildSoakReport(
  db: DB,
  config: Config,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): Record<string, unknown> {
  const range = opts.range ?? '7d';
  const perf = getPerformanceAnalytics(db, { range, mode: config.mode });
  const funnel = getFunnelAnalytics(db, { range });
  return {
    generatedAt: new Date().toISOString(),
    mode: config.mode,
    range,
    feeAware: true,
    sampleSize: perf.tradeCount,
    performance: perf,
    funnel,
    checkFailRates: listCheckFailRates(db, { range }),
    relaxedRisk: getRelaxedRiskAnalytics(db, { range }),
    caveats: [
      'Expectancy and profit factor need adequate sample size (prefer 50+ closed trades).',
      'Paper/dry-run fees are estimated; live fees come from execution when recorded.',
      'Do not mix paper and live numbers without filtering by mode.',
    ],
  };
}

export function buildFunnelCsv(db: DB, opts: { range?: '24h' | '7d' | '30d' | 'all' } = {}): string {
  const rates = listCheckFailRates(db, opts);
  const headers = ['check_id', 'pass', 'fail', 'unknown', 'fail_rate_pct'];
  const lines = [headers.join(',')];
  for (const r of rates) {
    lines.push([r.checkId, r.pass, r.fail, r.unknown, r.failRatePct.toFixed(2)].join(','));
  }
  return lines.join('\n') + '\n';
}

/**
 * Per-section CSV exports. Each mirrors exactly what its dashboard panel
 * renders (same list function, same filters) so the download and the screen
 * never disagree. Row caps match the underlying list clamps; the unbounded
 * closed-trade history stays in the trade blotter.
 */

export function buildPnlCsv(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d'; track?: Track; mode?: Config['mode'] } = {},
): string {
  const points = getPnlSeries(db, {
    range: opts.range ?? '7d',
    ...(opts.track ? { track: opts.track } : {}),
    ...(opts.mode ? { mode: opts.mode } : {}),
  });
  const headers = ['time', 'mint', 'pnl_sol', 'cumulative_pnl_sol'];
  const lines = [headers.join(',')];
  for (const p of points) {
    lines.push([p.time, p.mint, String(p.pnlSol), String(p.cumulativePnlSol)].map(csvEscape).join(','));
  }
  return lines.join('\n') + '\n';
}

export function buildPositionsCsv(
  db: DB,
  opts: { state?: 'open' | 'closed' | 'all'; limit?: number; track?: DataTrack; mode?: Config['mode'] } = {},
): string {
  const rows = listPositions(db, {
    ...(opts.state ? { state: opts.state } : {}),
    ...(opts.limit !== undefined ? { limit: opts.limit } : { limit: 200 }),
    ...(opts.track ? { track: opts.track } : {}),
    ...(opts.mode ? { mode: opts.mode } : {}),
  });
  const headers = [
    'id', 'mint', 'state', 'entry_tx', 'entry_price', 'exit_price', 'size_sol',
    'exit_reason', 'exit_tx', 'pnl_sol', 'pnl_pct', 'gross_pnl_sol', 'fees_sol',
    'net_pnl_sol', 'entry_soft_score', 'high_volatility', 'mfe_pct', 'mae_pct',
    'hold_ms', 'exit_trigger_to_confirm_ms', 'feed_source', 'venue', 'mode',
    'relaxed_risk', 'relaxed_reasons', 'unrealized_sol', 'opened_at', 'closed_at', 'created_at',
  ];
  const lines = [headers.join(',')];
  for (const r of rows) {
    const row: Record<string, unknown> = {
      id: r.id, mint: r.mint, state: r.state, entry_tx: r.entryTx, entry_price: r.entryPrice,
      exit_price: r.exitPrice, size_sol: r.sizeSol, exit_reason: r.exitReason, exit_tx: r.exitTx,
      pnl_sol: r.pnlSol, pnl_pct: r.pnlPct, gross_pnl_sol: r.grossPnlSol, fees_sol: r.feesSol,
      net_pnl_sol: r.netPnlSol, entry_soft_score: r.entrySoftScore, high_volatility: r.highVolatility,
      mfe_pct: r.mfePct, mae_pct: r.maePct, hold_ms: r.holdMs,
      exit_trigger_to_confirm_ms: r.exitTriggerToConfirmMs, feed_source: r.feedSource, venue: r.venue,
      mode: r.mode, relaxed_risk: r.relaxedRisk, relaxed_reasons: r.relaxedReasons.join(';'),
      unrealized_sol: r.unrealizedSol, opened_at: r.openedAt, closed_at: r.closedAt, created_at: r.createdAt,
    };
    lines.push(headers.map((h) => csvEscape(row[h])).join(','));
  }
  return lines.join('\n') + '\n';
}

export function buildCandidatesCsv(db: DB, opts: { limit?: number } = {}): string {
  const rows = listCandidates(db, opts.limit !== undefined ? { limit: opts.limit } : { limit: 150 });
  const headers = [
    'id', 'mint', 'verdict', 'soft_score', 'veto_reasons', 'high_volatility',
    'relaxed_risk', 'relaxed_reasons', 'hard_checks', 'created_at',
  ];
  const lines = [headers.join(',')];
  for (const r of rows) {
    const row: Record<string, unknown> = {
      id: r.id, mint: r.mint, verdict: r.verdict, soft_score: r.softScore,
      veto_reasons: r.vetoReasons.join(';'), high_volatility: r.highVolatility,
      relaxed_risk: r.relaxedRisk, relaxed_reasons: r.relaxedReasons.join(';'),
      hard_checks: r.hardChecks ? JSON.stringify(r.hardChecks) : '', created_at: r.createdAt,
    };
    lines.push(headers.map((h) => csvEscape(row[h])).join(','));
  }
  return lines.join('\n') + '\n';
}

export function buildEventsCsv(
  db: DB,
  opts: { limit?: number; level?: OperatorEventLevel; category?: string } = {},
): string {
  const rows = listEvents(db, {
    ...(opts.limit !== undefined ? { limit: opts.limit } : { limit: 300 }),
    ...(opts.level ? { level: opts.level } : {}),
    ...(opts.category ? { category: opts.category } : {}),
  });
  const headers = ['id', 'category', 'level', 'message', 'entity_mint', 'payload', 'created_at'];
  const lines = [headers.join(',')];
  for (const r of rows) {
    const row: Record<string, unknown> = {
      id: r.id, category: r.category, level: r.level, message: r.message,
      entity_mint: r.entityMint,
      payload: r.payload === null || r.payload === undefined ? '' : JSON.stringify(r.payload),
      created_at: r.createdAt,
    };
    lines.push(headers.map((h) => csvEscape(row[h])).join(','));
  }
  return lines.join('\n') + '\n';
}

export function buildBreakersCsv(db: DB, opts: { limit?: number } = {}): string {
  const rows = listBreakers(db, opts.limit !== undefined ? { limit: opts.limit } : { limit: 200 });
  const headers = ['type', 'detail', 'tripped', 'at'];
  const lines = [headers.join(',')];
  for (const r of rows) {
    lines.push([r.type, r.detail ?? '', String(r.tripped), r.at].map(csvEscape).join(','));
  }
  return lines.join('\n') + '\n';
}

export function buildVetoReasonsCsv(db: DB, opts: { range?: '24h' | '7d' | '30d' | 'all' } = {}): string {
  const breakdown = getVetoReasonBreakdown(db, opts);
  const lines = [`# total_vetoed=${breakdown.totalVetoed}`];
  const headers = ['scope', 'reason', 'category', 'count', 'pct'];
  lines.push(headers.join(','));
  const scopes: Array<{ scope: string; rows: VetoReasonRow[] }> = [
    { scope: 'primary', rows: breakdown.primary },
    { scope: 'all_reasons', rows: breakdown.allReasons },
  ];
  for (const s of scopes) for (const r of s.rows) {
    lines.push([s.scope, r.reason, r.category, String(r.count), r.pct.toFixed(2)].map(csvEscape).join(','));
  }
  return lines.join('\n') + '\n';
}

export function buildRelaxedRiskCsv(db: DB, opts: { range?: '24h' | '7d' | '30d' | 'all' } = {}): string {
  const a = getRelaxedRiskAnalytics(db, opts);
  const lines: string[] = [];
  lines.push('# Relaxed-risk rollout: clean vs relaxed cohorts');
  lines.push(
    `# rollback relaxed_net_pnl_sol=${a.rollback.relaxedNetPnlSol} ` +
      `relaxed_emergency_exits=${a.rollback.relaxedEmergencyExits} ` +
      `relaxed_stop_losses=${a.rollback.relaxedStopLosses} ` +
      `relaxed_failed_entries=${a.rollback.relaxedFailedEntries}`,
  );
  const headers = [
    'section', 'kind', 'accepted', 'entered', 'closed', 'win_rate_pct',
    'net_pnl_sol', 'emergency_exits', 'avg_mfe_pct', 'avg_mae_pct', 'reason', 'count',
  ];
  lines.push(headers.join(','));
  for (const g of a.groups) {
    const row: Record<string, unknown> = {
      section: 'group', kind: g.kind, accepted: g.accepted, entered: g.entered, closed: g.closed,
      win_rate_pct: g.winRatePct, net_pnl_sol: g.netPnlSol, emergency_exits: g.emergencyExits,
      avg_mfe_pct: g.avgMfePct, avg_mae_pct: g.avgMaePct, reason: '', count: '',
    };
    lines.push(headers.map((h) => csvEscape(row[h])).join(','));
  }
  for (const h of a.h4Reasons) {
    const row: Record<string, unknown> = {
      section: 'h4_reason', kind: '', accepted: '', entered: '', closed: '', win_rate_pct: '',
      net_pnl_sol: '', emergency_exits: '', avg_mfe_pct: '', avg_mae_pct: '', reason: h.reason, count: h.count,
    };
    lines.push(headers.map((k) => csvEscape(row[k])).join(','));
  }
  return lines.join('\n') + '\n';
}

/**
 * Raw latency samples (detection, confirm, slot-lag kinds) for offline
 * analysis. Percentiles stay on /api/latency; this is the underlying data.
 */
export function buildLatencyCsv(
  db: DB,
  opts: { kind?: string; range?: '24h' | '7d' | '30d' | 'all'; limit?: number } = {},
): string {
  const mod = rangeToModifier(opts.range);
  const limit = clampLimit(opts.limit, 5_000, 20_000);
  const clauses: string[] = [];
  const params: Array<string | number> = [];
  if (opts.kind) {
    clauses.push('kind = ?');
    params.push(opts.kind);
  }
  if (mod) {
    clauses.push(`julianday(created_at) >= julianday('now', ?)`);
    params.push(mod);
  }
  params.push(limit);
  const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
  const rows = db
    .prepare(
      `SELECT id, kind, mint, feed_source, latency_ms, created_at
       FROM latency_samples
       ${where}
       ORDER BY created_at ASC, id ASC
       LIMIT ?`,
    )
    .all(...params) as Array<{
    id: number; kind: string; mint: string | null; feed_source: string | null;
    latency_ms: number; created_at: string;
  }>;
  const headers = ['id', 'kind', 'mint', 'feed_source', 'latency_ms', 'created_at'];
  const lines = [headers.join(',')];
  for (const r of rows) {
    const row: Record<string, unknown> = {
      id: r.id, kind: r.kind, mint: r.mint, feed_source: r.feed_source,
      latency_ms: r.latency_ms, created_at: r.created_at,
    };
    lines.push(headers.map((h) => csvEscape(row[h])).join(','));
  }
  return lines.join('\n') + '\n';
}

export function buildRacerMigrationsCsv(db: DB, opts: { limit?: number } = {}): string {
  const rows = listRacerMigrations(db, opts.limit !== undefined ? { limit: opts.limit } : { limit: 500 });
  const headers = [
    'mint', 'signature', 'caller', 'tracked', 'fire_to_complete_slots', 'complete_to_migrate_slots',
    'failed_attempts', 'curve_buy_before', 'pool_buy_after', 'open_price', 'reach_price', 'at',
  ];
  const lines = [headers.join(',')];
  for (const r of rows) {
    const row: Record<string, unknown> = {
      mint: r.mint, signature: r.signature, caller: r.caller, tracked: r.tracked,
      fire_to_complete_slots: r.fireToComplete, complete_to_migrate_slots: r.completeToMigrate,
      failed_attempts: r.failedAttempts, curve_buy_before: r.curveBuyBefore, pool_buy_after: r.poolBuyAfter,
      open_price: r.openPrice, reach_price: r.reachPrice, at: r.at,
    };
    lines.push(headers.map((h) => csvEscape(row[h])).join(','));
  }
  return lines.join('\n') + '\n';
}

/**
 * One row per (paper track, horizon): the per-horizon PnL the racer table
 * renders, plus the full per-entry map as JSON for deeper analysis.
 */
export function buildRacerPaperCsv(db: DB, opts: { limit?: number } = {}): string {
  const rows = listRacerPaper(db, opts.limit !== undefined ? { limit: opts.limit } : { limit: 500 });
  const headers = [
    'mint', 'pool_address', 'size_sol', 'open_price', 'reach_price', 'armed',
    'live_cost_sol', 'live_cost_source', 'wallet_balance_sol', 'sizing_reason',
    'at', 'horizon_sec', 'price', 'quote_sol',
    'pnl_open_pct', 'pnl_reach_pct', 'pnl_by_entry', 'exit_price', 'rugged',
  ];
  const lines = [headers.join(',')];
  for (const r of rows) for (const h of r.horizons) {
    const row: Record<string, unknown> = {
      mint: r.mint, pool_address: r.poolAddress, size_sol: r.sizeSol, open_price: r.openPrice,
      reach_price: r.reachPrice, armed: r.armed, live_cost_sol: r.liveCostSol,
      live_cost_source: r.liveCostSource, wallet_balance_sol: r.walletBalanceSol ?? '',
      sizing_reason: r.sizingReason ?? '', at: r.at, horizon_sec: h.sec, price: h.price,
      quote_sol: h.quoteSol, pnl_open_pct: h.pnlOpenPct, pnl_reach_pct: h.pnlReachPct,
      pnl_by_entry: h.pnl ? JSON.stringify(h.pnl) : '', exit_price: h.exitPrice ?? '', rugged: h.rugged ?? false,
    };
    lines.push(headers.map((k) => csvEscape(row[k])).join(','));
  }
  return lines.join('\n') + '\n';
}

function computeUnrealizedPnl(db: DB, modeFilter = ''): number {
  const rows = db
    .prepare(
      `WITH latest AS (${latestPositionsSql()}),
            marks AS (
              SELECT mint, price
              FROM price_ticks
              WHERE rowid IN (SELECT MAX(rowid) FROM price_ticks GROUP BY mint)
            )
       SELECT l.size_sol AS sizeSol, l.entry_price AS entryPrice, m.price AS mark
       FROM latest l
       LEFT JOIN marks m ON m.mint = l.mint
       WHERE l.state IN ('OPEN', 'EXITING')${modeFilter}`,
    )
    .all() as Array<{ sizeSol: number; entryPrice: number | null; mark: number | null }>;

  let total = 0;
  for (const row of rows) {
    if (row.entryPrice && row.entryPrice > 0 && row.mark && row.mark > 0) {
      total += row.sizeSol * (row.mark / row.entryPrice - 1);
    }
  }
  return total;
}

function latestMarks(db: DB): Map<string, number> {
  const rows = db
    .prepare(
      `SELECT mint, price FROM price_ticks
       WHERE rowid IN (SELECT MAX(rowid) FROM price_ticks GROUP BY mint)`,
    )
    .all() as Array<{ mint: string; price: number }>;
  return new Map(rows.map((r) => [r.mint, r.price]));
}

/**
 * "Current state per mint" over an append-only positions table.
 *
 * The table name is a parameter so the dry-run twin table can reuse the exact
 * same shape. It is NEVER interpolated from user input — callers pass a literal
 * via `trackTable()`.
 */
function latestPositionsSql(table: PositionsTable = 'positions'): string {
  return `SELECT rowid AS id, * FROM ${table} WHERE rowid IN (SELECT MAX(rowid) FROM ${table} GROUP BY mint)`;
}

function mapPosition(row: Row, marks: Map<string, number>): DashboardPosition {
  const mint = String(row['mint']);
  const state = String(row['state']);
  const entryPrice = nullableNumber(row['entry_price']);
  const sizeSol = Number(row['size_sol']);
  let unrealizedSol: number | null = null;
  if ((state === 'OPEN' || state === 'EXITING') && entryPrice && entryPrice > 0) {
    const mark = marks.get(mint);
    if (mark && mark > 0) unrealizedSol = sizeSol * (mark / entryPrice - 1);
  }
  const hv = row['high_volatility'];
  return {
    id: Number(row['id']),
    mint,
    state,
    entryTx: nullableString(row['entry_tx']),
    entryPrice,
    exitPrice: nullableNumber(row['exit_price']),
    sizeSol,
    exitReason: nullableString(row['exit_reason']),
    exitTx: nullableString(row['exit_tx']),
    pnlSol: nullableNumber(row['pnl_sol']),
    pnlPct: nullableNumber(row['pnl_pct']),
    grossPnlSol: nullableNumber(row['gross_pnl_sol']),
    feesSol: nullableNumber(row['fees_sol']),
    netPnlSol: nullableNumber(row['net_pnl_sol']),
    entrySoftScore: nullableNumber(row['entry_soft_score']),
    highVolatility: hv === null || hv === undefined ? null : Number(hv) === 1,
    mfePct: nullableNumber(row['mfe_pct']),
    maePct: nullableNumber(row['mae_pct']),
    holdMs: nullableNumber(row['hold_ms']),
    exitTriggerToConfirmMs: nullableNumber(row['exit_trigger_to_confirm_ms']),
    feedSource: nullableString(row['feed_source']),
    venue: nullableString(row['venue']),
    mode: nullableString(row['mode']),
    relaxedRisk: Number(row['relaxed_risk'] ?? 0) === 1,
    relaxedReasons: parseJsonArray(nullableString(row['relaxed_reasons_json'])),
    openedAt: nullableString(row['opened_at']),
    closedAt: nullableString(row['closed_at']),
    createdAt: String(row['created_at']),
    unrealizedSol,
  };
}

function clampLimit(value: number | undefined, fallback: number, max: number): number {
  if (!value || !Number.isFinite(value)) return fallback;
  return Math.max(1, Math.min(Math.trunc(value), max));
}

function one<T>(db: DB, sql: string): T {
  return db.prepare(sql).get() as T;
}

function byRelaxed<T extends Record<string, unknown>>(rows: T[], countKey: keyof T): Map<'clean' | 'relaxed', number> {
  return new Map(
    rows.map((r) => [
      Number(r['relaxedRisk']) === 1 ? 'relaxed' : 'clean',
      typeof r[countKey] === 'number' ? r[countKey] : Number(r[countKey] ?? 0),
    ]),
  );
}

function nullableString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === 'number' ? value : null;
}

function parseJson(text: string | null): unknown {
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

function parseJsonArray(text: string | null): string[] {
  const parsed = parseJson(text);
  return Array.isArray(parsed) ? parsed.map(String) : [];
}

function parseHardChecks(
  text: string | null,
): Array<{ id: string; label?: string; status: string; detail?: string; reason?: string }> | undefined {
  const parsed = parseJson(text);
  if (!Array.isArray(parsed)) return undefined;
  return parsed.map((item) => {
    const o = item as Record<string, unknown>;
    return {
      id: String(o.id ?? ''),
      ...(typeof o.label === 'string' ? { label: o.label } : {}),
      status: String(o.status ?? 'unknown'),
      ...(typeof o.detail === 'string' ? { detail: o.detail } : {}),
      ...(typeof o.reason === 'string' ? { reason: o.reason } : {}),
    };
  });
}

export function csvEscape(value: unknown): string {
  if (value === null || value === undefined) return '';
  const s = String(value);
  if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}
