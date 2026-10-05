import { render } from 'preact';
import type { ComponentChildren } from 'preact';
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import {
  Chart,
  LineController,
  LineElement,
  PointElement,
  LinearScale,
  CategoryScale,
  Filler,
  Tooltip,
} from 'chart.js';
import '@fontsource-variable/dm-sans';
import './styles.css';

Chart.register(LineController, LineElement, PointElement, LinearScale, CategoryScale, Filler, Tooltip);

type Level = 'info' | 'warn' | 'error';

interface Summary {
  mode: string;
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
  flow: { graduations: number; accepted: number; vetoed: number; highVolatility: number };
  latency: {
    detection: { count: number; p50: number; p95: number; max: number };
    exitConfirm: { count: number; p50: number; p95: number; max: number };
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

interface RiskStatus {
  available?: boolean;
  mode?: string;
  killed?: boolean;
  streamDown?: boolean;
  dailyRealizedPnlSol?: number;
  dailyLossLimitSol?: number;
  dailyLossUsedPct?: number;
  consecutiveLosses?: number;
  consecutiveLossHalt?: number;
  emergencies24h?: number;
  emergencyExitCount24hLimit?: number;
  walletBalanceSol?: number | null;
  walletFloorSol?: number;
  tripped?: string[];
  canEnter?: { ok: boolean; reason?: string; detail?: string };
}

interface FunnelStats {
  graduations: number;
  accepted: number;
  vetoed: number;
  entered: number;
  closed: number;
  failed: number;
  acceptRatePct: number;
  entryRatePct: number;
}

interface VetoReasonRow {
  reason: string;
  category: 'unknown' | 'hard_fail' | 'low_score' | 'none';
  count: number;
  pct: number;
}

interface VetoBreakdown {
  totalVetoed: number;
  primary: VetoReasonRow[];
  allReasons: VetoReasonRow[];
}

interface EdgeCohortRow {
  cohort: string;
  n: number;
  netSol: number;
  meanPct: number;
  winRatePct: number;
}

/** /api/analytics/edge (work plan 2026-09-25 P4.3). */
interface EdgeAnalytics {
  track: 'live' | 'dry';
  window: number;
  n: number;
  expectancyPct: { point: number; lo: number; hi: number } | null;
  netSol: number;
  winRatePct: number | null;
  feePctOfNotional: number | null;
  emergencyShareOfLossesPct: number | null;
  cohorts: { byRisk: EdgeCohortRow[]; bySuffix: EdgeCohortRow[]; byExit: EdgeCohortRow[] };
  latency: Record<string, { count: number; p50: number; p95: number }>;
  calibration: Array<{ bin: string; n: number; meanProb: number; winRatePct: number }>;
  monitor: { n: number; negative: boolean; ci: { point: number; lo: number; hi: number } | null } | null;
}

interface RelaxedRiskAnalytics {
  groups: Array<{
    kind: 'clean' | 'relaxed';
    accepted: number;
    entered: number;
    closed: number;
    winRatePct: number;
    netPnlSol: number;
    emergencyExits: number;
    avgMfePct: number;
    avgMaePct: number;
  }>;
  h4Reasons: Array<{ reason: string; count: number }>;
  rollback: {
    relaxedNetPnlSol: number;
    relaxedEmergencyExits: number;
    relaxedStopLosses: number;
    relaxedFailedEntries: number;
  };
}

/** Live accepted vs capital-free veto dry-run performance (shadow tracker). */
interface VetoDryRunComparison {
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
  byVetoReason: Array<{
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
  }>;
  trackedN: number;
  exitSimN: number;
  legacyPeakN: number;
  vetoDryRunTotal: number;
}

/** Mint-level counterfactual dry-run outcome (from shadow_outcomes). */
interface ShadowOutcomeRow {
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

interface BreakerRow {
  type: string;
  detail: string | null;
  tripped: boolean;
  at: string;
}

interface SlotDist {
  n: number;
  p50: number | null;
  p90: number | null;
}

interface RacerSummary {
  curves: { admitted: number; fired: number; open: number; migrated: number; completeNoPool: number; expired: number };
  migrations: { total: number; tracked: number; fired: number; unpriced?: Record<string, number>; coverage?: Record<string, number> };
  slots: { fireToComplete: SlotDist; completeToMigrate: SlotDist; fireToMigrate: SlotDist };
  competition: { attempts: number; failedAttempts: number; failedPerMigration: number | null; contestedShare: number | null };
  execution?: { slot1BuyAttempts: number; slot1Failed: number; observedOnChainFillRate: number | null; offChainDropRate: null;
    slot0BuyVolumeSol: number | null; slot1BuyVolumeSol: number | null; slot0DistinctBuyerOwners: number; slot1DistinctBuyerOwners: number;
    meanFailedNetworkFeeSol: number | null };
  bundles: { curveBuyBeforeShare: number | null; poolBuyAfterShare: number | null };
  topCallers: Array<{ caller: string; wins: number; share: number }>;
  buyLeg: { okRate: number | null; avgMs: number | null };
  paper: {
    tracks: number;
    openPriced: number;
    realisticTracks?: number;
    verifiedTracks?: number;
    excludedReasons?: Record<string, number>;
    highDepletionThresholdPct?: number;
    armedShare?: number | null;
    ruggedTracks?: number;
    measuredCostShare?: number | null;
    medianLiveCostSol?: number | null;
    horizons: Array<{
      sec: number;
      n: number;
      medianOpenPct: number | null;
      medianReachPct: number | null;
      winRateOpen: number | null;
      winRateReach: number | null;
      entries?: Record<string, { n: number; median: number | null; winRate: number | null }>;
      headline?: { mean: number | null; meanWithoutBest: number | null; ciLow: number | null; ciHigh: number | null; cumulativeFixedSizePnlSol: number; fixedSizePnlCurve?: Array<{ mint: string; exitAt: string; pnlSol: number; cumulativePnlSol: number }>; fixedSizeCurveUnavailable?: number };
      cohorts?: {
        belowHighDepletion: { n: number; mean: number | null; median: number | null; winRate: number | null };
        highDepletion: { n: number; mean: number | null; median: number | null; winRate: number | null };
        unknownDepletion?: { n: number; mean: number | null; median: number | null; winRate: number | null };
        byCaller: Array<{ caller: string; stats: { n: number; mean: number | null; median: number | null; winRate: number | null } }>;
      };
      featureSplits?: Array<{ feature: string; cohort: string; stats: { n: number; mean: number | null; median: number | null; winRate: number | null; ciLow: number | null; ciHigh: number | null } }>;
      sizeScenario?: { sizeSol: number; priced: number; unavailable: number; pnlSol: number; peakConcurrentSol: number; ataRentLockedSol: number; onChainFillAdjustedPnlSol: number | null };
    }>;
    pathRules?: Array<{ rule: string; n: number; unpriced: number; notCaptured?: number; unchangedFills?: number; result: { n: number; mean: number | null; median: number | null; winRate: number | null; ciLow: number | null; ciHigh: number | null }; sameTradeTimeBaseline: { n: number; mean: number | null; median: number | null; winRate: number | null } }>;
    evaluation?: { verifiedCompleted: number; exploratoryRows: number; reservedLaterRows: number; selectionReady: boolean; validated: false };
  };
  latency?: {
    detection: { racerSlots: SlotDist; liveMs: SlotDist; liveSlots: SlotDist };
    sending: {
      sendReadyMs: SlotDist;
      stagesMs: { waitPool: SlotDist; state: SlotDist; build: SlotDist; sign: SlotDist };
      liveDetectToSendMs: SlotDist;
    };
    landing: {
      modeledEntrySlot: number;
      feasibleOffsetSlots?: SlotDist;
      migrateSlotStillOpenShare?: number | null;
      firstOutsideBuySlots: SlotDist;
      sameSlotBuyShare: number | null;
      onChainFillRate: number | null;
      liveEntryLandSlots: SlotDist;
      liveEntryConfirmMs: SlotDist;
    };
    exit: {
      assumedExitLandMs: number | null;
      horizonsFromStream?: number;
      horizonsFromRpc?: number;
      horizonNextSlotFills?: number;
      horizonUnchangedFills?: number;
      unchangedFillShare: number | null;
      liveExitTriggerToSendMs: SlotDist;
      liveExitSendToLandedMs: SlotDist;
      liveExitLandSlots: SlotDist;
    };
  };
  lastMigrateAt: string | null;
  lastCurveAt: string | null;
}

interface RacerPaperRow {
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
    rugged?: boolean;
  }>;
  armed?: boolean | null;
  liveCostSol?: number | null;
  liveCostSource?: string | null;
  entryQuality?: string;
  sameSlotDepletionPct?: number | null;
  migrationCaller?: string | null;
  at: string;
}

interface RacerMigrationRow {
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
  unpricedReason?: string | null;
  initialQuoteDepthSol?: number | null;
  observedQuoteDepthSol?: number | null;
  depthThresholdSol?: number | null;
  migrateSlot?: number | null;
  receiptSlot?: number | null;
  feasibleSlot?: number | null;
  sendReadyUs?: number | null;
  at: string;
}

interface PositionRow {
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
  grossPnlSol?: number | null;
  feesSol?: number | null;
  netPnlSol?: number | null;
  entrySoftScore?: number | null;
  highVolatility?: boolean | null;
  mfePct?: number | null;
  maePct?: number | null;
  holdMs?: number | null;
  exitTriggerToConfirmMs?: number | null;
  feedSource?: string | null;
  venue?: string | null;
  unrealizedSol?: number | null;
  relaxedRisk?: boolean;
  relaxedReasons?: string[];
  openedAt: string | null;
  closedAt: string | null;
  createdAt: string;
}

interface PnlPoint {
  time: string;
  mint: string;
  pnlSol: number;
  cumulativePnlSol: number;
}

interface CandidateRow {
  id: number;
  mint: string;
  verdict: string | null;
  softScore: number | null;
  vetoReasons: string[];
  highVolatility: boolean;
  createdAt: string;
  hardChecks?: Array<{ id: string; label?: string; status: string; detail?: string; reason?: string }>;
  relaxedRisk?: boolean;
  relaxedReasons?: string[];
}

interface OperatorEvent {
  id: number;
  category: string;
  level: Level;
  message: string;
  entityMint: string | null;
  payload?: unknown;
  createdAt: string;
}

type PositionFilter = 'open' | 'closed' | 'all';
type PnlRange = '24h' | '7d' | '30d';
type AnalyticsRange = '24h' | '7d' | '30d' | 'all';
type DashboardStatus = 'connecting' | 'live' | 'offline' | 'reconnecting';
type Theme = 'dark' | 'light';

/**
 * Which leg of the dual-track run the workspace is showing.
 *   live  — real capital
 *   dry   — the ideal paper twin opened alongside every accepted candidate
 *   delta — live minus dry: total execution drag
 */
type Track = 'live' | 'dry' | 'delta';

type DragCohort = 'both' | 'dry_only' | 'live_only';

/** Top-level workspace: the scalper (live / dry-run / Δ tracks) or the migration racer. */
type View = 'scalper' | 'racer';
const VIEW_LABELS: Record<View, string> = { scalper: 'Scalper', racer: 'Migration racer' };

/** `#racer` deep-links the racer tab; otherwise the last choice, else the scalper. */
function initialView(): View {
  if (typeof window === 'undefined') return 'scalper';
  if (window.location.hash === '#racer') return 'racer';
  if (window.location.hash === '#scalper') return 'scalper';
  try {
    return window.localStorage.getItem('pumpdesk-view') === 'racer' ? 'racer' : 'scalper';
  } catch {
    return 'scalper';
  }
}

interface ExecutionDragRow {
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

interface ExecutionDrag {
  range: AnalyticsRange;
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
  byDryExitReason: Array<{ exitReason: string; n: number; netPnlDeltaSol: number; medianHoldMsDelta: number | null }>;
  caveats: string[];
  rows: ExecutionDragRow[];
}

const emptyDrag: ExecutionDrag = {
  range: '7d',
  sessionScoped: false,
  cohorts: { bothN: 0, dryOnlyN: 0, liveOnlyN: 0, incompleteN: 0, unknownStatusN: 0 },
  totals: {
    netPnlDeltaSol: 0,
    netPnlDeltaSolSizeAdj: 0,
    feeDeltaSol: 0,
    opportunityCostSol: 0,
    liveNetPnlSol: 0,
    dryNetPnlSol: 0,
  },
  perTrade: {
    avgNetPnlDeltaSol: null,
    medianNetPnlDeltaSol: null,
    avgEntryDragPct: null,
    avgExitDragPct: null,
    medianHoldMsDelta: null,
    avgExitConfirmMs: null,
    exitTriggerMatchPct: null,
  },
  winRate: { livePct: null, dryPct: null, n: 0 },
  byLiveStatus: [],
  byDryExitReason: [],
  caveats: [],
  rows: [],
};

const TRACK_LABELS: Record<Track, string> = { live: 'Live', dry: 'Dry-run', delta: 'Δ' };

const emptyLatency = { count: 0, p50: 0, p95: 0, max: 0 };

const emptySummary: Summary = {
  mode: 'paper',
  pnl: {
    realizedSol: 0,
    realized24hSol: 0,
    realized7dSol: 0,
    winRatePct: 0,
    closedCount: 0,
    wins: 0,
    losses: 0,
    expectancySol: 0,
    profitFactor: 0,
    maxDrawdownSol: 0,
    currentDrawdownSol: 0,
    avgWinSol: 0,
    avgLossSol: 0,
    feesSol: 0,
    unrealizedSol: 0,
  },
  positions: { openCount: 0, openExposureSol: 0, maxConcurrent: 0, pendingCount: 0, exitingCount: 0, failedCount: 0 },
  flow: { graduations: 0, accepted: 0, vetoed: 0, highVolatility: 0 },
  latency: {
    detection: emptyLatency,
    exitConfirm: emptyLatency,
    detectionSlots: emptyLatency,
    entryLandSlots: emptyLatency,
    exitLandSlots: emptyLatency,
  },
  system: { latestBreaker: null, latestEventAt: null, lastGraduationAt: null },
};

function App() {
  const [theme, setTheme] = useState<Theme>('dark');
  const [summary, setSummary] = useState<Summary>(emptySummary);
  const [positions, setPositions] = useState<PositionRow[]>([]);
  const [pnl, setPnl] = useState<PnlPoint[]>([]);
  const [candidates, setCandidates] = useState<CandidateRow[]>([]);
  const [events, setEvents] = useState<OperatorEvent[]>([]);
  const [risk, setRisk] = useState<RiskStatus | null>(null);
  const [funnel, setFunnel] = useState<FunnelStats | null>(null);
  const [vetoBreakdown, setVetoBreakdown] = useState<VetoBreakdown | null>(null);
  const [vetoDryRun, setVetoDryRun] = useState<VetoDryRunComparison | null>(null);
  const [shadowOutcomes, setShadowOutcomes] = useState<ShadowOutcomeRow[]>([]);
  const [relaxedRisk, setRelaxedRisk] = useState<RelaxedRiskAnalytics | null>(null);
  const [edge, setEdge] = useState<EdgeAnalytics | null>(null);
  const [walletRec, setWalletRec] = useState<WalletReconciliation | null>(null);
  const [breakers, setBreakers] = useState<BreakerRow[]>([]);
  const [racerSummary, setRacerSummary] = useState<RacerSummary | null>(null);
  const [racerEvents, setRacerEvents] = useState<RacerMigrationRow[]>([]);
  const [racerPaper, setRacerPaper] = useState<RacerPaperRow[]>([]);
  const [view, setView] = useState<View>(initialView);
  const chooseView = (next: View) => {
    setView(next);
    try {
      window.localStorage.setItem('pumpdesk-view', next);
    } catch {
      /* per-viewer convenience only */
    }
    if (typeof window !== 'undefined' && window.history?.replaceState) window.history.replaceState(null, '', `#${next}`);
  };
  const [drag, setDrag] = useState<ExecutionDrag>(emptyDrag);
  const [positionFilter, setPositionFilter] = useState<PositionFilter>('open');
  const [pnlRange, setPnlRange] = useState<PnlRange>('7d');
  const [vetoDryRunRange, setVetoDryRunRange] = useState<AnalyticsRange>('7d');
  const [track, setTrack] = useState<Track>('live');
  // A dry-run bot opens on the Dry-run tab (its numbers live there); a manual
  // tab choice is never overridden.
  const trackChosen = useRef(false);
  const chooseTrack = (next: Track) => {
    trackChosen.current = true;
    setTrack(next);
  };
  const [selectedDrag, setSelectedDrag] = useState<ExecutionDragRow | null>(null);
  const [status, setStatus] = useState<DashboardStatus>('connecting');
  const [streamReady, setStreamReady] = useState(false);
  const [manualRefreshing, setManualRefreshing] = useState(false);
  const [selectedPosition, setSelectedPosition] = useState<PositionRow | null>(null);
  const [selectedEvent, setSelectedEvent] = useState<OperatorEvent | null>(null);
  const [selectedCandidate, setSelectedCandidate] = useState<CandidateRow | null>(null);
  const [selectedShadow, setSelectedShadow] = useState<ShadowOutcomeRow | null>(null);
  const [authOpen, setAuthOpen] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [loggingIn, setLoggingIn] = useState(false);

  useEffect(() => {
    const stored = window.localStorage.getItem('pumpdesk-theme');
    const next: Theme = stored === 'light' ? 'light' : 'dark';
    setTheme(next);
    document.documentElement.classList.toggle('light', next === 'light');
    document.documentElement.style.colorScheme = next;
  }, []);

  const toggleTheme = () => {
    setTheme((current) => {
      const next: Theme = current === 'dark' ? 'light' : 'dark';
      window.localStorage.setItem('pumpdesk-theme', next);
      document.documentElement.classList.toggle('light', next === 'light');
      document.documentElement.style.colorScheme = next;
      return next;
    });
  };

  const refresh = async () => {
    const health = await fetchJson<{ mode?: string }>('/api/health');
    if (!trackChosen.current && health?.mode === 'dry-run' && track === 'live') {
      trackChosen.current = true;
      setTrack('dry'); // the effect re-runs refresh for the dry tab
      return;
    }
    // Δ mode reads wallet-shaped panels from the live track; the drag numbers
    // come from /api/analytics/execution-drag, whose type is unambiguous.
    const dataTrack = track === 'delta' ? 'live' : track;
    // Keyed rather than a positional tuple: a 14-element destructure is one
    // careless reorder away from silently swapping two panels' data.
    const res = await allKeyed({
      summary: fetchJson<Summary>(`/api/dashboard/summary?track=${dataTrack}`),
      positions: fetchJson<PositionRow[]>(`/api/positions?state=${positionFilter}&limit=40&track=${dataTrack}`),
      pnl: fetchJson<PnlPoint[]>(`/api/pnl?range=${pnlRange}&track=${dataTrack}`),
      drag: fetchJson<ExecutionDrag>(`/api/analytics/execution-drag?range=${pnlRange}`),
      candidates: fetchJson<CandidateRow[]>('/api/candidates?limit=12'),
      events: fetchJson<OperatorEvent[]>('/api/events?limit=40'),
      risk: fetchJson<RiskStatus>('/api/risk/status'),
      funnel: fetchJson<FunnelStats>('/api/analytics/funnel?range=24h'),
      veto: fetchJson<VetoBreakdown>('/api/analytics/veto-reasons?range=24h'),
      vetoDryRun: fetchJson<VetoDryRunComparison>(
        `/api/analytics/veto-dry-run-comparison?range=${vetoDryRunRange}`,
      ),
      shadow: fetchJson<ShadowOutcomeRow[]>(`/api/shadow-outcomes?range=${vetoDryRunRange}&limit=80`),
      relaxed: fetchJson<RelaxedRiskAnalytics>('/api/analytics/relaxed-risk?range=24h'),
      breakers: fetchJson<BreakerRow[]>('/api/breakers?limit=8'),
      racerSummary: fetchJson<RacerSummary>('/api/racer/summary'),
      racerEvents: fetchJson<RacerMigrationRow[]>('/api/racer/migrations?limit=50'),
      racerPaper: fetchJson<RacerPaperRow[]>('/api/racer/paper?limit=30'),
      // Optional panel: an older server without the route must not take the
      // whole refresh down.
      edge: fetchJson<EdgeAnalytics>(`/api/analytics/edge?track=${dataTrack}&window=100`).catch((err: unknown) => {
        if (err instanceof AuthError) throw err;
        return null;
      }),
      walletRec: fetchJson<WalletReconciliation>('/api/wallet/reconciliation').catch((err: unknown) => {
        if (err instanceof AuthError) throw err;
        return null;
      }),
    });

    setSummary(normalizeSummary(res.summary));
    setPositions(Array.isArray(res.positions) ? res.positions.map(normalizePosition) : []);
    setPnl(Array.isArray(res.pnl) ? res.pnl.map(normalizePnlPoint) : []);
    setDrag(res.drag ?? emptyDrag);
    setCandidates(Array.isArray(res.candidates) ? res.candidates.map(normalizeCandidate) : []);
    setEvents(Array.isArray(res.events) ? res.events : []);
    setRisk(res.risk ?? null);
    setFunnel(res.funnel ?? null);
    setVetoBreakdown(res.veto ?? null);
    setVetoDryRun(res.vetoDryRun ?? null);
    setShadowOutcomes(Array.isArray(res.shadow) ? res.shadow.map(normalizeShadowOutcome) : []);
    setRelaxedRisk(res.relaxed ?? null);
    setEdge(res.edge ?? null);
    setWalletRec(res.walletRec ?? null);
    setBreakers(Array.isArray(res.breakers) ? res.breakers : []);
    setRacerSummary(res.racerSummary ?? null);
    setRacerEvents(Array.isArray(res.racerEvents) ? res.racerEvents : []);
    setRacerPaper(Array.isArray(res.racerPaper) ? res.racerPaper : []);
    setStatus('live');
    setStreamReady(true);
  };

  const handleManualRefresh = async () => {
    if (manualRefreshing) return;
    setManualRefreshing(true);
    try {
      await refresh();
    } catch (error) {
      handleRequestError(error);
    } finally {
      setManualRefreshing(false);
    }
  };

  /** 401 means the dashboard is locked (or the stored password rotated): pop
      the in-app login instead of the browser's native dialog. Anything else is
      the API being down. */
  const handleRequestError = (error: unknown) => {
    if (error instanceof AuthError) {
      setAuthOpen(true);
      setStatus('connecting');
      setStreamReady(false);
      return;
    }
    setStatus('offline');
    setStreamReady(false);
  };

  const handleLogin = async (username: string, password: string) => {
    setLoggingIn(true);
    setLoginError(null);
    try {
      // Probe with the candidate credentials before storing anything, so a
      // typo reports "invalid" instead of opening the dashboard half-loaded.
      const candidate = encodeBasicCredentials(username, password);
      const probe = await fetch('/api/health', { headers: { authorization: `Basic ${candidate}` } });
      if (probe.status === 401) {
        setLoginError('Invalid username or password.');
        return;
      }
      if (!probe.ok) throw new Error(`/api/health ${probe.status}`);
      storeAuthToken(candidate);
      setAuthOpen(false);
      await refresh();
    } catch (error) {
      if (error instanceof AuthError) {
        setLoginError('Invalid username or password.');
      } else {
        setLoginError('Dashboard API offline. Start the bot dashboard on 127.0.0.1:8787.');
      }
    } finally {
      setLoggingIn(false);
    }
  };

  useEffect(() => {
    let disposed = false;
    const run = async () => {
      try {
        await refresh();
      } catch (error) {
        if (!disposed) handleRequestError(error);
      }
    };

    setStreamReady(false);
    void run();
    const timer = setInterval(() => void run(), 15_000);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, [positionFilter, pnlRange, vetoDryRunRange, track]);

  useEffect(() => {
    if (!streamReady || status !== 'live' || authOpen) return;

    // fetch-based SSE, not EventSource: EventSource cannot send the
    // Authorization header, so with auth on it would 401 forever.
    let disposed = false;
    const controller = new AbortController();
    const unsubscribe = subscribeStream({
      signal: controller.signal,
      onReady: () => {
        if (!disposed) setStatus('live');
      },
      onEvent: (next) => {
        if (disposed) return;
        setEvents((current) => [next, ...current].slice(0, 40));
        void refresh().catch(handleRequestError);
      },
      onError: (error) => {
        if (disposed) return;
        if (error instanceof AuthError) {
          handleRequestError(error);
          return;
        }
        setStatus('reconnecting');
        setStreamReady(false);
      },
    });
    return () => {
      disposed = true;
      unsubscribe();
      controller.abort();
    };
    // `refresh` is captured in this closure, so every filter it reads must be a
    // dep. Without `track` an SSE-triggered refresh would refetch the PREVIOUS
    // track and clobber the one just selected. `vetoDryRunRange` was already
    // missing here for the same reason.
  }, [streamReady, status, authOpen, positionFilter, pnlRange, vetoDryRunRange, track]);

  const modalOpen =
    selectedPosition !== null ||
    selectedEvent !== null ||
    selectedCandidate !== null ||
    selectedShadow !== null ||
    selectedDrag !== null;
  const closeDetails = () => {
    setSelectedPosition(null);
    setSelectedEvent(null);
    setSelectedCandidate(null);
    setSelectedShadow(null);
    setSelectedDrag(null);
  };

  useEffect(() => {
    if (!modalOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeDetails();
    };

    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [modalOpen]);

  const notifications = useMemo(() => events.filter((e) => e.category === 'notification').slice(0, 8), [events]);
  const finalPnl = pnl.at(-1)?.cumulativePnlSol ?? summary.pnl.realizedSol;
  /**
   * Delta curves are built from the PAIRED rows only, not from /api/pnl?track=dry.
   * The dry track also contains twins live never traded, so plotting it whole
   * would make the visible gap disagree with the Total drag KPI. Same trades on
   * both curves means the gap IS totals.netPnlDeltaSol.
   */
  const deltaSeries = useMemo(() => {
    const paired = drag.rows
      .filter((r) => r.cohort === 'both' && r.netPnlDeltaSol !== null)
      .slice()
      .sort((a, b) => (a.at ?? '').localeCompare(b.at ?? ''));
    let live = 0;
    let dry = 0;
    const livePoints: PnlPoint[] = [];
    const dryPoints: PnlPoint[] = [];
    for (const row of paired) {
      live += row.liveNetPnlSol ?? 0;
      dry += row.dryNetPnlSol ?? 0;
      const time = row.at ?? '';
      livePoints.push({ time, mint: row.mint, pnlSol: row.liveNetPnlSol ?? 0, cumulativePnlSol: live });
      dryPoints.push({ time, mint: row.mint, pnlSol: row.dryNetPnlSol ?? 0, cumulativePnlSol: dry });
    }
    return { livePoints, dryPoints };
  }, [drag]);

  const chartHeadline = track === 'delta' ? drag.totals.netPnlDeltaSol : finalPnl;
  // Δ mode reads wallet-shaped panels from the live track; exports follow suit.
  const dataTrack = track === 'delta' ? 'live' : track;

  return (
    <div class="app-stage">
      <div class="dashboard-frame">
        <Sidebar theme={theme} onToggleTheme={toggleTheme} view={view} onView={chooseView} />

        <main class={`workspace ${view === 'racer' ? 'racer-view' : ''}`}>
          <header class="topbar">
            <div class="breadcrumb">
              <span>Dashboard</span>
              <b>/</b>
              <strong>
                {view === 'racer' ? 'Migration racer' : track === 'dry' && summary.mode === 'dry-run' ? 'Dry-run PnL' : 'Wallet PnL'}
              </strong>
            </div>
            <div class="account-strip">
              <Segmented value={view} values={['scalper', 'racer']} labels={VIEW_LABELS} onChange={chooseView} />
              {view === 'scalper' && (
                <Segmented value={track} values={['live', 'dry', 'delta']} labels={TRACK_LABELS} onChange={chooseTrack} />
              )}
              {view === 'scalper' ? (
              <div class="export-row">
                <ExportLink
                  href={`/api/reports/trades.csv?range=7d&track=${dataTrack}`}
                  onUnauthorized={() => setAuthOpen(true)}
                >
                  Trades CSV
                </ExportLink>
                {track === 'delta' && (
                  <ExportLink href="/api/reports/execution-drag.csv?range=7d" onUnauthorized={() => setAuthOpen(true)}>
                    Drag CSV
                  </ExportLink>
                )}
                <ExportLink href="/api/reports/ops.json" onUnauthorized={() => setAuthOpen(true)}>
                  Ops JSON
                </ExportLink>
                <ExportLink href="/api/reports/soak.json?range=7d" onUnauthorized={() => setAuthOpen(true)}>
                  Soak JSON
                </ExportLink>
                <ExportLink
                  href={`/api/reports/full-bundle.json?range=${pnlRange}&track=${dataTrack}`}
                  onUnauthorized={() => setAuthOpen(true)}
                >
                  Bundle JSON
                </ExportLink>
              </div>
              ) : (
              <div class="export-row">
                <ExportLink href="/api/reports/racer-migrations.csv" onUnauthorized={() => setAuthOpen(true)}>
                  Migrations CSV
                </ExportLink>
                <ExportLink href="/api/reports/racer-paper.csv" onUnauthorized={() => setAuthOpen(true)}>
                  Paper CSV
                </ExportLink>
                <ExportLink
                  href={`/api/reports/full-bundle.json?range=${pnlRange}&track=${dataTrack}`}
                  onUnauthorized={() => setAuthOpen(true)}
                >
                  Bundle JSON
                </ExportLink>
              </div>
              )}
              <button type="button" class="refresh-button" onClick={handleManualRefresh} disabled={manualRefreshing}>
                {manualRefreshing ? 'Refreshing' : 'Refresh'}
              </button>
              <span class={`connection ${status}`} />
              <span>{status}</span>
              <strong>{summary.mode}</strong>
              <div class="avatar">PF</div>
            </div>
          </header>

          {status !== 'live' && (
            <div class="offline-strip" role="status">
              {authOpen
                ? 'Authentication required. Sign in to load the dashboard.'
                : 'Dashboard API offline. Start the bot dashboard on 127.0.0.1:8787.'}
            </div>
          )}

          {view === 'racer' ? (
            <RacerWorkspace
              summary={racerSummary}
              migrations={racerEvents}
              paper={racerPaper}
              onUnauthorized={() => setAuthOpen(true)}
            />
          ) : (
          <>
          {/* Banner and KPI row share ONE grid row. `.workspace` has a fixed
              grid-template-rows, so a bare extra child would shift every
              section down a row and clip the metric grid. */}
          <div class="metrics-zone">
          {/* The Δ track shows execution-drag differences, not balances — say so
              plainly. A dry-run bot's numbers live on the Dry-run tab under a
              banner; Live carries no banner (real capital only). */}
          {track === 'dry' && summary.mode === 'dry-run' && (
            <div class="track-strip dry-run-strip" role="status">
              Dry-run — simulated trades only. Nothing is sent on-chain; PnL is modelled from pool prices, not the wallet.
            </div>
          )}
          {track === 'delta' && (
            <div class="track-strip" role="status">
              Δ track — live minus dry-run. These are execution-drag differences, not wallet balances.
            </div>
          )}

          {track === 'delta' ? (
            <DragKpis drag={drag} />
          ) : (
          <section class="metric-grid metric-grid-wide" aria-label="Wallet metrics">
            <KpiCard
              label="Realized PnL"
              value={formatSol(summary.pnl.realizedSol)}
              detail={`${summary.pnl.closedCount} closed · fees ${formatSol(summary.pnl.feesSol)}`}
              tone={summary.pnl.realizedSol >= 0 ? 'profit' : 'loss'}
            />
            {track === 'live' && walletRec?.available && (
              <KpiCard
                label="Wallet vs Ledger"
                value={formatSol(walletRec.walletDeltaSol)}
                detail={`ledger ${formatSol(walletRec.explainedSol)} · unexplained ${formatSol(walletRec.unexplainedSol)} · since ${walletRec.anchor ? new Date(walletRec.anchor.at).toLocaleDateString() : '—'}`}
                tone={Math.abs(walletRec.unexplainedSol) <= 0.01 ? 'profit' : 'loss'}
              />
            )}
            <KpiCard
              label="Unrealized"
              value={formatSol(summary.pnl.unrealizedSol)}
              detail={`${summary.positions.openCount} open / ${summary.positions.exitingCount} exiting`}
              tone={summary.pnl.unrealizedSol >= 0 ? 'profit' : 'loss'}
            />
            <KpiCard
              label="24h PnL"
              value={formatSol(summary.pnl.realized24hSol)}
              detail={`7d ${formatSol(summary.pnl.realized7dSol)}`}
              tone={summary.pnl.realized24hSol >= 0 ? 'profit' : 'loss'}
            />
            <KpiCard
              label="Open Exposure"
              value={`${summary.positions.openExposureSol.toFixed(3)} SOL`}
              detail={`${summary.positions.openCount}/${summary.positions.maxConcurrent} · pending ${summary.positions.pendingCount}`}
            />
            <KpiCard
              label="Win Rate"
              value={`${summary.pnl.winRatePct.toFixed(0)}%`}
              detail={`${summary.pnl.wins}W / ${summary.pnl.losses}L · exp ${formatSol(summary.pnl.expectancySol)}`}
              tone={summary.pnl.winRatePct >= 50 ? 'profit' : 'loss'}
            />
            <KpiCard
              label="Exit p95"
              value={`${summary.latency.exitConfirm.p95.toFixed(0)} ms`}
              detail={`n=${summary.latency.exitConfirm.count} · det p95 ${summary.latency.detection.p95.toFixed(0)} ms · land p50 ${summary.latency.exitLandSlots.p50.toFixed(0)} slots`}
            />
            <KpiCard
              label="Chain Lag"
              value={`${summary.latency.detectionSlots.p50.toFixed(0)} slots`}
              detail={`det n=${summary.latency.detectionSlots.count} · p95 ${summary.latency.detectionSlots.p95.toFixed(0)} · entry land p50 ${summary.latency.entryLandSlots.p50.toFixed(0)} slots`}
            />
            <KpiCard
              label="Max Drawdown"
              value={formatSol(-Math.abs(summary.pnl.maxDrawdownSol))}
              detail={`current ${formatSol(-Math.abs(summary.pnl.currentDrawdownSol))} · PF ${summary.pnl.profitFactor.toFixed(2)}`}
              tone="loss"
            />
          </section>
          )}
          </div>

          <section class="content-grid">
            <Panel
              title={track === 'delta' ? 'Live vs Dry-run' : 'PnL vs Time'}
              wide
              action={
                <div class="panel-actions">
                  <Segmented value={pnlRange} values={['24h', '7d', '30d']} onChange={setPnlRange} />
                  <ExportLink
                    href={`/api/reports/pnl.csv?range=${pnlRange}&track=${track}`}
                    onUnauthorized={() => setAuthOpen(true)}
                  >
                    PnL CSV
                  </ExportLink>
                </div>
              }
            >
              <div class="chart-header">
                <div>
                  <span>{track === 'delta' ? 'Execution drag' : 'Current curve'}</span>
                  <strong class={chartHeadline >= 0 ? 'profit-text' : 'loss-text'}>{formatSol(chartHeadline)}</strong>
                </div>
                <div class="legend">
                  <span class={track === 'delta' ? 'legend-dot live' : 'legend-dot pnl'} />
                  <span>{track === 'delta' ? 'Live' : 'Realized PnL'}</span>
                  {track === 'delta' && (
                    <>
                      <span class="legend-dot dry" />
                      <span>Dry-run</span>
                    </>
                  )}
                </div>
              </div>
              {/* Two curves in Δ mode: the GAP between them is the drag, read
                  directly off the chart without doing arithmetic. */}
              <PnlChart
                datasets={
                  track === 'delta'
                    ? [
                        { label: 'Live', points: deltaSeries.livePoints, color: theme === 'light' ? '#000000' : '#ffffff' },
                        { label: 'Dry-run', points: deltaSeries.dryPoints, color: '#22c55e' },
                      ]
                    : [{ label: 'Realized PnL', points: pnl, color: finalPnl >= 0 ? '#22c55e' : '#ef4444', fill: true }]
                }
              />
            </Panel>

            <Panel
              title="Risk & Ops"
              action={
                <ExportLink href="/api/reports/ops.json" onUnauthorized={() => setAuthOpen(true)}>
                  Ops JSON
                </ExportLink>
              }
            >
              <RiskPanel risk={risk} summary={summary} funnel={funnel} />
            </Panel>
          </section>

          <section class="table-zone">
            {track === 'delta' ? (
              <Panel
                title="Execution drag per trade"
                action={
                  <div class="panel-actions">
                    <DragCoverageNote drag={drag} />
                    <ExportLink
                      href={`/api/reports/execution-drag.csv?range=${pnlRange}`}
                      onUnauthorized={() => setAuthOpen(true)}
                    >
                      Drag CSV
                    </ExportLink>
                  </div>
                }
              >
                <DragTable rows={drag.rows} onSelect={setSelectedDrag} />
              </Panel>
            ) : (
              <Panel
                title={track === 'dry' ? (summary.mode === 'dry-run' ? 'Dry-run positions' : 'Dry-run twins') : 'Positions'}
                action={
                  <div class="panel-actions">
                    <Segmented value={positionFilter} values={['open', 'closed', 'all']} onChange={setPositionFilter} />
                    <ExportLink
                      href={`/api/reports/positions.csv?state=${positionFilter}&track=${dataTrack}`}
                      onUnauthorized={() => setAuthOpen(true)}
                    >
                      Positions CSV
                    </ExportLink>
                  </div>
                }
              >
                <PositionsTable rows={positions} onSelect={setSelectedPosition} />
              </Panel>
            )}
          </section>

          <section class="lower-grid">
            <Panel
              title="Notifications"
              action={
                <div class="export-row">
                  <ExportLink
                    href="/api/reports/events.csv?category=notification&limit=300"
                    onUnauthorized={() => setAuthOpen(true)}
                  >
                    Notifications CSV
                  </ExportLink>
                  <ExportLink href="/api/reports/events.csv?limit=300" onUnauthorized={() => setAuthOpen(true)}>
                    All events CSV
                  </ExportLink>
                </div>
              }
            >
              <EventList rows={notifications} empty="No notifications yet" compact onSelect={setSelectedEvent} />
            </Panel>
            <Panel
              title="Recent Guardrails"
              action={
                <ExportLink href="/api/reports/candidates.csv?limit=150" onUnauthorized={() => setAuthOpen(true)}>
                  Candidates CSV
                </ExportLink>
              }
            >
              <Candidates rows={candidates} onSelect={setSelectedCandidate} />
            </Panel>
            <Panel
              title="Breakers"
              action={
                <ExportLink href="/api/reports/breakers.csv?limit=200" onUnauthorized={() => setAuthOpen(true)}>
                  Breakers CSV
                </ExportLink>
              }
            >
              <BreakerList rows={breakers} />
            </Panel>
          </section>

          <section class="table-zone">
            <Panel
              title="Why candidates were vetoed (24h)"
              action={
                <div class="export-row">
                  <ExportLink href="/api/reports/funnel.csv?range=24h" onUnauthorized={() => setAuthOpen(true)}>
                    Checks CSV
                  </ExportLink>
                  <ExportLink href="/api/reports/veto-reasons.csv?range=24h" onUnauthorized={() => setAuthOpen(true)}>
                    Reasons CSV
                  </ExportLink>
                </div>
              }
            >
              <VetoReasonsPanel breakdown={vetoBreakdown} funnel={funnel} />
            </Panel>
          </section>

          <section class="table-zone analytics-zone">
            <Panel
              title="Veto dry-runs"
              action={
                <div class="panel-actions">
                  <Segmented
                    value={vetoDryRunRange}
                    values={['24h', '7d', '30d', 'all']}
                    onChange={setVetoDryRunRange}
                  />
                  <div class="export-row">
                    <ExportLink
                      href={`/api/reports/veto-dry-run-summary.csv?range=${vetoDryRunRange}`}
                      onUnauthorized={() => setAuthOpen(true)}
                    >
                      Summary CSV
                    </ExportLink>
                    <ExportLink
                      href={`/api/reports/veto-dry-run.csv?range=${vetoDryRunRange}`}
                      onUnauthorized={() => setAuthOpen(true)}
                    >
                      Detail CSV
                    </ExportLink>
                  </div>
                </div>
              }
            >
              <VetoDryRunPanel
                comparison={vetoDryRun}
                rows={shadowOutcomes}
                onSelect={setSelectedShadow}
              />
            </Panel>
          </section>

          <section class="table-zone">
            <Panel
              title="Edge — last 100 closed trades"
              action={
                <div class="export-row">
                  <ExportLink
                    href={`/api/reports/edge.csv?track=${dataTrack}&window=100`}
                    onUnauthorized={() => setAuthOpen(true)}
                  >
                    Edge CSV
                  </ExportLink>
                  <ExportLink href="/api/reports/latency.csv?range=7d" onUnauthorized={() => setAuthOpen(true)}>
                    Latency CSV
                  </ExportLink>
                </div>
              }
            >
              <EdgePanel edge={edge} />
            </Panel>
          </section>

          <section class="table-zone">
            <Panel
              title="Relaxed-risk rollout (24h)"
              action={
                <ExportLink href="/api/reports/relaxed-risk.csv?range=24h" onUnauthorized={() => setAuthOpen(true)}>
                  Rollout CSV
                </ExportLink>
              }
            >
              <RelaxedRiskPanel analytics={relaxedRisk} />
            </Panel>
          </section>
          </>
          )}
        </main>
      </div>

      {selectedPosition && <PositionModal position={selectedPosition} onClose={closeDetails} />}
      {selectedEvent && <EventModal event={selectedEvent} onClose={closeDetails} />}
      {selectedCandidate && <CandidateModal candidate={selectedCandidate} onClose={closeDetails} />}
      {selectedShadow && <ShadowOutcomeModal outcome={selectedShadow} onClose={closeDetails} />}
      {selectedDrag && <DragModal row={selectedDrag} onClose={closeDetails} />}
      {authOpen && <AuthModal busy={loggingIn} error={loginError} onSubmit={(u, p) => void handleLogin(u, p)} />}
    </div>
  );
}

function Sidebar({
  theme,
  onToggleTheme,
  view,
  onView,
}: {
  theme: Theme;
  onToggleTheme: () => void;
  view: View;
  onView: (v: View) => void;
}) {
  return (
    <aside class="sidebar">
      <div class="brand-row">
        <div class="brand-mark">PF.</div>
        <div>
          <h1>PumpDesk</h1>
          <span>Operator UI</span>
        </div>
      </div>
      <nav class="sidebar-nav" aria-label="Workspaces">
        {(['scalper', 'racer'] as const).map((v) => (
          <a
            href={`#${v}`}
            class={view === v ? 'active' : ''}
            aria-current={view === v ? 'page' : undefined}
            onClick={(e) => {
              e.preventDefault();
              onView(v);
            }}
          >
            {VIEW_LABELS[v]}
          </a>
        ))}
      </nav>
      <div class="sidebar-footer">
        <nav class="sidebar-links" aria-label="Creator links">
          <span class="sidebar-links-label">Creator</span>
          <a href="https://www.nishimweprince.dev/" target="_blank" rel="noopener noreferrer">Website</a>
          <a href="https://github.com/nishimweprince" target="_blank" rel="noopener noreferrer">GitHub</a>
          <a href="https://github.com/nishimweprince/trading-algos/tree/main/pump-fun" target="_blank" rel="noopener noreferrer">Source repo</a>
        </nav>
        <button
          type="button"
          class="theme-toggle"
          onClick={onToggleTheme}
          aria-pressed={theme === 'dark'}
          aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        >
          <span class={`theme-switch ${theme === 'dark' ? 'is-dark' : ''}`}><i /></span>
          <span aria-hidden="true">{theme === 'dark' ? '◔' : '☼'}</span>
          <span>{theme}</span>
        </button>
      </div>
    </aside>
  );
}

/** /api/wallet/reconciliation — wallet SOL change vs what the ledger explains. */
interface WalletReconciliation {
  available: boolean;
  anchor: { sol: number; at: string } | null;
  latest: { sol: number; at: string } | null;
  walletDeltaSol: number;
  closedPnlSol: number;
  closedModelPnlSol: number;
  closedCount: number;
  closedMissingActuals: number;
  openCostSol: number;
  openCount: number;
  rentSol: number;
  explainedSol: number;
  unexplainedSol: number;
}

function KpiCard(props: { label: string; value: string; detail: string; tone?: 'profit' | 'loss' | undefined }) {
  return (
    <article class="kpi-card">
      <div class="kpi-title">
        <span>{props.label}</span>
      </div>
      <div class="kpi-body">
        <div>
          <strong class={props.tone === 'profit' ? 'profit-text' : props.tone === 'loss' ? 'loss-text' : ''}>{props.value}</strong>
          <span>{props.detail}</span>
        </div>
      </div>
    </article>
  );
}

function Panel(props: { title: string; action?: ComponentChildren; children: ComponentChildren; wide?: boolean }) {
  return (
    <section class={`panel ${props.wide ? 'wide' : ''}`}>
      <div class="panel-head">
        <h2>{props.title}</h2>
        {props.action}
      </div>
      {props.children}
    </section>
  );
}

/** `labels` is optional so existing call sites keep rendering the raw value. */
function Segmented<T extends string>(props: {
  value: T;
  values: T[];
  labels?: Record<string, string>;
  onChange: (value: T) => void;
}) {
  return (
    <div class="segmented">
      {props.values.map((value) => (
        <button type="button" class={props.value === value ? 'active' : ''} onClick={() => props.onChange(value)}>
          {props.labels?.[value] ?? value}
        </button>
      ))}
    </div>
  );
}

/**
 * In-app login popup. Deliberately NOT dismissible (no backdrop-click, no
 * close button, Escape does nothing): while it is open the API answers 401 to
 * everything, so there is nothing useful behind it.
 */
function AuthModal({
  busy,
  error,
  onSubmit,
}: {
  busy: boolean;
  error: string | null;
  onSubmit: (username: string, password: string) => void;
}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const userRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    userRef.current?.focus();
  }, []);

  const submit = (event: Event) => {
    event.preventDefault();
    if (busy || username.trim() === '' || password === '') return;
    onSubmit(username.trim(), password);
  };

  return (
    <div class="modal-backdrop">
      <section class="position-modal auth-modal" role="dialog" aria-modal="true" aria-labelledby="auth-modal-title">
        <header class="modal-head">
          <div>
            <span>Authentication required</span>
            <h2 id="auth-modal-title">Sign in to PumpDesk</h2>
          </div>
        </header>
        <form class="auth-form" onSubmit={submit}>
          <p class="auth-hint">Enter the dashboard credentials to continue.</p>
          <label class="auth-field">
            <span>Username</span>
            <input
              ref={userRef}
              type="text"
              name="username"
              autoComplete="username"
              value={username}
              onInput={(e) => setUsername(e.currentTarget.value)}
              disabled={busy}
            />
          </label>
          <label class="auth-field">
            <span>Password</span>
            <input
              type="password"
              name="password"
              autoComplete="current-password"
              value={password}
              onInput={(e) => setPassword(e.currentTarget.value)}
              disabled={busy}
            />
          </label>
          {error && (
            <p class="auth-error" role="alert">
              {error}
            </p>
          )}
          <button
            type="submit"
            class="refresh-button auth-submit"
            disabled={busy || username.trim() === '' || password === ''}
          >
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </section>
    </div>
  );
}

/**
 * Export link that carries the login credentials. A plain `<a href>` cannot
 * send the Authorization header, so with auth on it would download a 401
 * JSON file (and, before the server stopped sending `WWW-Authenticate:
 * Basic`, summon the native browser login). Keeps the href so the URL stays
 * visible/copyable; auth off behaves exactly like before.
 */
function ExportLink({
  href,
  onUnauthorized,
  children,
}: {
  href: string;
  onUnauthorized: () => void;
  children: ComponentChildren;
}) {
  const download = async (event: { preventDefault: () => void }) => {
    event.preventDefault();
    try {
      const res = await authedFetch(href);
      if (res.status === 401) {
        onUnauthorized();
        return;
      }
      if (!res.ok) throw new Error(`${href} ${res.status}`);
      const blob = await res.blob();
      const filename =
        filenameFromDisposition(res.headers.get('content-disposition')) ??
        href.split('/').at(-1)?.split('?')[0] ??
        'download';
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
    } catch {
      // Offline / API down: the status strip already reports it; there is
      // nothing to download, so stay silent.
    }
  };

  return (
    <a class="export-link" href={href} onClick={(event) => void download(event)}>
      {children}
    </a>
  );
}

interface ChartSeries {
  label: string;
  points: PnlPoint[];
  color: string;
  fill?: boolean;
}

/**
 * Takes N series so Δ mode can plot live and dry together — the gap between the
 * curves is the execution drag, read without arithmetic. Labels come from the
 * LONGEST series so a shorter one doesn't truncate the x-axis.
 */
function PnlChart({ datasets, labelFormat = formatMonthish }: { datasets: ChartSeries[]; labelFormat?: (time: string) => string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const multi = datasets.length > 1;

  useEffect(() => {
    if (!canvasRef.current) return;
    const styles = getComputedStyle(document.documentElement);
    const chartText = styles.getPropertyValue('--muted').trim();
    const chartGrid = styles.getPropertyValue('--rule-soft').trim();
    const panel = styles.getPropertyValue('--panel').trim();
    const ink = styles.getPropertyValue('--ink').trim();
    const labelSource = datasets.reduce(
      (longest, d) => (d.points.length > longest.length ? d.points : longest),
      [] as PnlPoint[],
    );
    chartRef.current?.destroy();
    chartRef.current = new Chart(canvasRef.current, {
      type: 'line',
      data: {
        labels: labelSource.map((p) => labelFormat(p.time)),
        datasets: datasets.map((d) => ({
          label: d.label,
          data: d.points.map((p) => p.cumulativePnlSol),
          borderColor: d.color,
          backgroundColor: d.fill ? `${d.color}1a` : 'transparent',
          fill: d.fill ?? false,
          tension: 0.34,
          pointRadius: 2,
          pointHoverRadius: 4,
          borderWidth: 2,
        })),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          tooltip: {
            displayColors: multi,
            backgroundColor: panel,
            titleColor: ink,
            bodyColor: chartText,
            borderColor: chartGrid,
            borderWidth: 1,
          },
          legend: { display: false },
        },
        scales: {
          x: { ticks: { maxTicksLimit: 7, color: chartText }, grid: { display: false } },
          y: { ticks: { color: chartText }, grid: { color: chartGrid } },
        },
      },
    });
    return () => chartRef.current?.destroy();
  }, [datasets, labelFormat]);

  if (datasets.every((d) => d.points.length === 0)) {
    return <div class="empty chart-empty">No closed positions in this window</div>;
  }
  return (
    <div class="chart-box">
      <canvas ref={canvasRef} class="chart" />
    </div>
  );
}

/**
 * Δ-mode KPI row. Every value answers one question: what is execution costing?
 * Negative drag means live underperformed the ideal, so it reads as a loss.
 */
function DragKpis({ drag }: { drag: ExecutionDrag }) {
  const { perTrade, totals, cohorts, winRate } = drag;
  const avg = perTrade.avgNetPnlDeltaSol;
  const entry = perTrade.avgEntryDragPct;
  const exit = perTrade.avgExitDragPct;
  return (
    <section class="metric-grid metric-grid-wide" aria-label="Execution drag metrics">
      <KpiCard
        label="Drag / trade"
        value={avg === null ? '—' : formatSol(avg)}
        detail={`median ${perTrade.medianNetPnlDeltaSol === null ? '—' : formatSol(perTrade.medianNetPnlDeltaSol)} · n=${cohorts.bothN}`}
        tone={avg === null ? undefined : avg >= 0 ? 'profit' : 'loss'}
      />
      <KpiCard
        label="Total drag"
        value={formatSol(totals.netPnlDeltaSol)}
        detail={`live ${formatSol(totals.liveNetPnlSol)} vs dry ${formatSol(totals.dryNetPnlSol)}`}
        tone={totals.netPnlDeltaSol >= 0 ? 'profit' : 'loss'}
      />
      <KpiCard
        label="Fee drag (est.)"
        value={formatSol(totals.feeDeltaSol)}
        detail="estimated on both legs — reflects fill-count differences"
        tone={totals.feeDeltaSol <= 0 ? 'profit' : 'loss'}
      />
      <KpiCard
        label="Slippage + latency"
        value={entry === null ? '—' : `${entry >= 0 ? '+' : ''}${entry.toFixed(2)}% entry`}
        detail={`${exit === null ? '—' : `${exit >= 0 ? '+' : ''}${exit.toFixed(2)}% exit`} · confirm ${perTrade.avgExitConfirmMs === null ? '—' : `${perTrade.avgExitConfirmMs.toFixed(0)} ms`}`}
        tone={entry === null ? undefined : entry <= 0 ? 'profit' : 'loss'}
      />
      <KpiCard
        label="Hold-time drag"
        value={perTrade.medianHoldMsDelta === null ? '—' : formatHoldMs(perTrade.medianHoldMsDelta)}
        detail={`exit trigger match ${perTrade.exitTriggerMatchPct === null ? '—' : `${perTrade.exitTriggerMatchPct.toFixed(0)}%`}`}
        tone={perTrade.medianHoldMsDelta === null ? undefined : perTrade.medianHoldMsDelta <= 0 ? 'profit' : 'loss'}
      />
      <KpiCard
        label="Dry vs Live win rate"
        value={
          winRate.dryPct === null || winRate.livePct === null
            ? '—'
            : `${winRate.dryPct.toFixed(0)}% vs ${winRate.livePct.toFixed(0)}%`
        }
        detail={`n=${winRate.n} paired trades`}
        tone={
          winRate.dryPct === null || winRate.livePct === null
            ? undefined
            : winRate.livePct >= winRate.dryPct
              ? 'profit'
              : 'loss'
        }
      />
      <KpiCard
        label="Opportunity cost"
        value={formatSol(totals.opportunityCostSol)}
        detail={`${cohorts.dryOnlyN} not traded · ${cohorts.liveOnlyN} uncovered · ${cohorts.incompleteN} in flight`}
        tone={totals.opportunityCostSol >= 0 ? 'loss' : 'profit'}
      />
    </section>
  );
}

/**
 * Coverage is bounded (capacity drops, missing pricing, trades predating the
 * twin). Say so in the panel head rather than letting a partial sample read as
 * a complete one.
 */
function DragCoverageNote({ drag }: { drag: ExecutionDrag }) {
  const { liveOnlyN, incompleteN, unknownStatusN } = drag.cohorts;
  if (liveOnlyN === 0 && incompleteN === 0 && unknownStatusN === 0) {
    return <span class="panel-note-inline">{drag.cohorts.bothN} paired · full coverage</span>;
  }
  const parts: string[] = [];
  if (liveOnlyN > 0) parts.push(`${liveOnlyN} live without a twin`);
  if (incompleteN > 0) parts.push(`${incompleteN} still in flight`);
  if (unknownStatusN > 0) parts.push(`${unknownStatusN} unattributed`);
  return <span class="panel-note-inline">Coverage gap: {parts.join(' · ')}</span>;
}

const LIVE_STATUS_PILL: Record<string, string> = {
  entered: 'accept',
  pending_entry: 'veto-unknown',
  blocked_concurrent: 'veto-low_score',
  blocked_relaxed_cap: 'veto-low_score',
  blocked_breaker: 'veto-low_score',
  blocked_kill_switch: 'veto-hard_fail',
  entry_failed: 'veto',
  no_executor: 'veto-unknown',
  unknown: 'veto-unknown',
};

function DragTable({ rows, onSelect }: { rows: ExecutionDragRow[]; onSelect: (row: ExecutionDragRow) => void }) {
  if (rows.length === 0) {
    return <div class="empty">No paired trades yet. The twin opens alongside each accepted candidate.</div>;
  }
  return (
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Mint</th>
            <th>Live status</th>
            <th>Size</th>
            <th>Live net</th>
            <th>Dry net</th>
            <th>Δ net</th>
            <th>Δ hold</th>
            <th>Live / Dry exit</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.mint}
              class="position-row"
              role="button"
              tabIndex={0}
              onClick={() => onSelect(row)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  onSelect(row);
                }
              }}
            >
              <td class="mono">{short(row.mint)}</td>
              <td>
                <span class={`pill ${LIVE_STATUS_PILL[row.liveStatus ?? 'unknown'] ?? 'veto-unknown'}`}>
                  {row.liveStatus ?? (row.cohort === 'live_only' ? 'no twin' : 'unknown')}
                </span>
              </td>
              <td>{row.liveSizeSol ?? row.drySizeSol ?? '—'}</td>
              <td class={toneClass(row.liveNetPnlSol)}>{row.liveNetPnlSol === null ? '—' : formatSol(row.liveNetPnlSol)}</td>
              <td class={toneClass(row.dryNetPnlSol)}>{row.dryNetPnlSol === null ? '—' : formatSol(row.dryNetPnlSol)}</td>
              <td class={toneClass(row.netPnlDeltaSol)}>
                {row.netPnlDeltaSol === null ? '—' : formatSol(row.netPnlDeltaSol)}
              </td>
              <td class={toneClass(row.holdMsDelta === null ? null : -row.holdMsDelta)}>
                {row.holdMsDelta === null ? '—' : formatHoldMs(row.holdMsDelta)}
              </td>
              <td class="mono breakable">
                {row.liveExitReason ?? '—'} / {row.dryExitReason ?? '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DragModal({ row, onClose }: { row: ExecutionDragRow; onClose: () => void }) {
  return (
    <div
      class="modal-backdrop"
      onClick={(e) => {
        if (e.currentTarget === e.target) onClose();
      }}
    >
      <div class="position-modal" role="dialog" aria-modal="true">
        <div class="modal-head">
          <h2>Execution drag</h2>
          <button type="button" class="refresh-button" onClick={onClose}>
            Close
          </button>
        </div>
        <div class="mint-panel">
          <span class="mono breakable">{row.mint}</span>
          <PumpFunLink href={pumpFunUrl(row.mint)} />
        </div>
        <div class="detail-grid">
          <DetailRow label="Cohort" value={row.cohort} />
          <DetailRow label="Live status" value={row.liveStatus ?? '—'} />
          <DetailRow label="Live net PnL" value={row.liveNetPnlSol === null ? '—' : formatSol(row.liveNetPnlSol)} />
          <DetailRow label="Dry net PnL" value={row.dryNetPnlSol === null ? '—' : formatSol(row.dryNetPnlSol)} />
          <DetailRow label="Δ net PnL" value={row.netPnlDeltaSol === null ? '—' : formatSol(row.netPnlDeltaSol)} />
          <DetailRow
            label="Δ net (size-adj)"
            value={row.netPnlDeltaSolSizeAdj === null ? '—' : formatSol(row.netPnlDeltaSolSizeAdj)}
          />
          <DetailRow label="Δ PnL %" value={row.pnlPctDelta === null ? '—' : `${row.pnlPctDelta.toFixed(2)}%`} />
          <DetailRow label="Entry drag" value={row.entryDragPct === null ? '—' : `${row.entryDragPct.toFixed(3)}%`} />
          <DetailRow label="Exit drag" value={row.exitDragPct === null ? '—' : `${row.exitDragPct.toFixed(3)}%`} />
          <DetailRow label="Fee Δ (est.)" value={row.feeDeltaSol === null ? '—' : formatSol(row.feeDeltaSol)} />
          <DetailRow label="Fill count Δ" value={row.fillCountDelta === null ? '—' : String(row.fillCountDelta)} />
          <DetailRow label="Hold Δ" value={row.holdMsDelta === null ? '—' : formatHoldMs(row.holdMsDelta)} />
          <DetailRow
            label="Exit confirm"
            value={row.exitConfirmMs === null ? '—' : `${row.exitConfirmMs.toFixed(0)} ms`}
          />
          <DetailRow label="MFE Δ" value={row.mfeDeltaPct === null ? '—' : `${row.mfeDeltaPct.toFixed(2)}%`} />
          <DetailRow label="Live exit" value={row.liveExitReason ?? '—'} />
          <DetailRow label="Dry exit" value={row.dryExitReason ?? '—'} />
          <DetailRow
            label="Exit trigger match"
            value={row.exitTriggerMatch === null ? '—' : row.exitTriggerMatch ? 'yes' : 'no'}
          />
          <DetailRow label="Live size" value={row.liveSizeSol === null ? '—' : `${row.liveSizeSol} SOL`} />
          <DetailRow label="Dry size" value={row.drySizeSol === null ? '—' : `${row.drySizeSol} SOL`} />
          <DetailRow label="At" value={formatOptionalTime(row.at)} />
        </div>
        <p class="modal-note">
          Fees are estimated on both legs, so the fee Δ reflects differing fill counts rather than observed on-chain
          fees. The total Δ is still correct — live entry and exit prices are real fills.
        </p>
      </div>
    </div>
  );
}

function toneClass(value: number | null): string {
  if (value === null) return '';
  return value >= 0 ? 'profit-text' : 'loss-text';
}

function RiskPanel({
  risk,
  summary,
  funnel,
}: {
  risk: RiskStatus | null;
  summary: Summary;
  funnel: FunnelStats | null;
}) {
  const breaker = summary.system.latestBreaker;
  const live = risk && risk.available !== false && risk.killed !== undefined;
  const dayUsed = live ? zeroNumber(risk.dailyLossUsedPct) : 0;
  const canEnter = live ? risk.canEnter?.ok !== false : true;

  return (
    <div class="recommendations">
      <div class="recommendation">
        <div>
          <strong>Entry gate</strong>
          <span>{live ? (canEnter ? 'Open for new entries' : risk.canEnter?.detail ?? risk.canEnter?.reason ?? 'Blocked') : 'Risk snapshot offline (standalone dashboard)'}</span>
        </div>
        <b class={canEnter ? 'profit-text' : 'loss-text'}>{live ? (canEnter ? 'OK' : 'BLOCK') : 'n/a'}</b>
      </div>
      <div class="recommendation">
        <div>
          <strong>Daily loss</strong>
          <span>
            {live
              ? `${formatSol(risk.dailyRealizedPnlSol)} / limit -${zeroNumber(risk.dailyLossLimitSol).toFixed(3)} SOL`
              : '—'}
          </span>
        </div>
        <b>{dayUsed.toFixed(0)}%</b>
      </div>
      <div class="recommendation">
        <div>
          <strong>Streak / emergencies</strong>
          <span>
            {live
              ? `${zeroNumber(risk.consecutiveLosses)}/${zeroNumber(risk.consecutiveLossHalt)} losses · ${zeroNumber(risk.emergencies24h)}/${zeroNumber(risk.emergencyExitCount24hLimit)} emerg 24h`
              : `${summary.positions.failedCount} failed positions`}
          </span>
        </div>
        <b class={live && risk.killed ? 'loss-text' : ''}>{live && risk.killed ? 'KILL' : live && risk.streamDown ? 'STREAM' : 'clear'}</b>
      </div>
      <div class="recommendation">
        <div>
          <strong>Funnel 24h</strong>
          <span>
            {funnel
              ? `${funnel.graduations} grad → ${funnel.accepted} accept → ${funnel.entered} enter → ${funnel.closed} close`
              : `${summary.flow.graduations} grad · ${summary.flow.accepted} accept · ${summary.flow.vetoed} veto`}
          </span>
        </div>
        <b>{funnel ? `${funnel.acceptRatePct.toFixed(0)}%` : `${summary.flow.highVolatility} HV`}</b>
      </div>
      <div class="recommendation">
        <div>
          <strong>Breaker</strong>
          <span>{breaker ? breaker.detail ?? breaker.type : 'No breaker events'}</span>
        </div>
        <b class={breaker?.tripped ? 'loss-text' : ''}>{breaker?.tripped ? 'Tripped' : 'Clear'}</b>
      </div>
      <div class="total-box">
        Wallet {live && risk.walletBalanceSol != null ? `${risk.walletBalanceSol.toFixed(3)} SOL` : 'n/a'}
        {live && risk.walletFloorSol != null ? ` · floor ${risk.walletFloorSol.toFixed(3)}` : ''}
        {summary.system.lastGraduationAt ? ` · last grad ${formatTime(summary.system.lastGraduationAt)}` : ''}
      </div>
    </div>
  );
}

function fmtPct(v: number | null | undefined, digits = 0): string {
  return v === null || v === undefined ? '—' : `${(v * 100).toFixed(digits)}%`;
}

function fmtDist(d: SlotDist): string {
  if (d.n === 0 || d.p50 === null) return '—';
  return `p50 ${d.p50.toFixed(1)} · p90 ${d.p90 === null ? '—' : d.p90.toFixed(1)} (n=${d.n})`;
}

function fmtSignedPct(v: number | null): string {
  return v === null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`;
}

function pnlTone(v: number | null): 'profit' | 'loss' | undefined {
  return v === null ? undefined : v >= 0 ? 'profit' : 'loss';
}

/** Realistic entry columns: first in slot +1, the median position in slots +1..+3, and the per-migration feasible slot. */
const REALISTIC_KEYS = ['slot1', 'slot1mid', 'slot2mid', 'slot3mid', 'feasiblemid'] as const;
const REALISTIC_LABELS: Record<(typeof REALISTIC_KEYS)[number], string> = {
  slot1: '+1 first',
  slot1mid: '+1 mid',
  slot2mid: '+2 mid',
  slot3mid: '+3 mid',
  feasiblemid: 'feasible mid',
};
/** The headline leg: landing mid-pack in the slot after the migrate. */
const HEADLINE_KEY = 'slot1mid';

function PnlCell({ v }: { v: number | null }) {
  return <td class={v === null ? '' : v >= 0 ? 'profit-text' : 'loss-text'}>{fmtSignedPct(v)}</td>;
}

type LatencySource = 'measured' | 'proxy' | 'assumption' | 'live' | 'none';

const SOURCE_TAG: Record<LatencySource, string> = {
  measured: 'measured',
  proxy: 'proxy',
  assumption: 'assumption',
  live: 'live',
  none: 'no data',
};

const SOURCE_LABEL: Record<LatencySource, string> = {
  measured: 'measured · racer',
  proxy: 'proxy · other bots on-chain',
  assumption: 'assumption · config',
  live: 'live trader samples',
  none: 'no data yet',
};

function fmtMs(d: SlotDist | undefined): string {
  if (!d || d.n === 0 || d.p50 === null) return '—';
  const f = (v: number) => (v >= 100 ? v.toFixed(0) : v.toFixed(1));
  return `p50 ${f(d.p50)} ms · p90 ${d.p90 === null ? '—' : f(d.p90)} ms · n=${d.n}`;
}

function fmtSlots(d: SlotDist | undefined): string {
  if (!d || d.n === 0 || d.p50 === null) return '—';
  return `p50 ${d.p50.toFixed(1)} · p90 ${d.p90 === null ? '—' : d.p90.toFixed(1)} slots · n=${d.n}`;
}

/** One stage of the detection → send → land → exit chain. */
function LatencyCard(props: {
  stage: string;
  value: string;
  caption: string;
  source: LatencySource;
  rows: Array<[string, string, LatencySource]>;
}) {
  return (
    <article class="latency-card">
      <header>
        <span class="latency-stage">{props.stage}</span>
        <span class={`latency-source src-${props.source}`}>{SOURCE_LABEL[props.source]}</span>
      </header>
      <strong>{props.value}</strong>
      <span class="latency-caption">{props.caption}</span>
      <table class="kv-table latency-rows">
        <tbody>
          {props.rows.map(([k, v, src]) => (
            <tr key={k}>
              <th>{k}</th>
              <td>
                <span>{v}</span>
                <em class={`src-${src}`}>{SOURCE_TAG[src]}</em>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </article>
  );
}

/** Label / value pairs as a real two-column table (readable at any width). */
function KvTable({ rows }: { rows: Array<[string, ComponentChildren]> }) {
  return (
    <table class="kv-table">
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k}>
            <th>{k}</th>
            <td>{v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RacerLatencyPanel({ summary }: { summary: RacerSummary }) {
  const l = summary.latency;
  if (!l) return null;
  const has = (d: SlotDist | undefined) => !!d && d.n > 0;
  const det = l.detection.racerSlots;
  const detSrc: LatencySource = has(det) ? 'measured' : 'none';
  const send = l.sending.liveDetectToSendMs;
  const ready = l.sending.sendReadyMs;
  const st = l.sending.stagesMs;
  const sendSrc: LatencySource = has(ready) ? 'measured' : has(send) ? 'live' : 'none';
  const first = l.landing.firstOutsideBuySlots;
  const exitLive = l.exit.liveExitSendToLandedMs;
  return (
    <section class="latency-grid" aria-label="Latency chain">
      <LatencyCard
        stage="1 · Detection"
        value={has(det) ? `${det.p50!.toFixed(1)} slots` : '—'}
        caption="Heartbeat slot when the landed migrate reached us − its slot (≤ 0: before the slot was marked processed)"
        source={detSrc}
        rows={[
          ['Racer migrate receipt', fmtSlots(det), detSrc],
          ['Trader feed → handler', fmtMs(l.detection.liveMs), has(l.detection.liveMs) ? 'live' : 'none'],
          ['Trader detection lag', fmtSlots(l.detection.liveSlots), has(l.detection.liveSlots) ? 'live' : 'none'],
        ]}
      />
      <LatencyCard
        stage="2 · Sending"
        value={has(ready) ? `${ready.p50!.toFixed(2)} ms` : '—'}
        caption="Landed-migrate frame receipt → signed post-migration buy (real local build, never sent). Target ≤ 10 ms."
        source={sendSrc}
        rows={[
          ['Send-ready (receipt → signed)', fmtMs(ready), has(ready) ? 'measured' : 'none'],
          ['Wait for pool push', fmtMs(st.waitPool), has(st.waitPool) ? 'measured' : 'none'],
          ['State / build / sign', has(st.build) ? `${st.state.p50!.toFixed(2)} / ${st.build.p50!.toFixed(2)} / ${st.sign.p50!.toFixed(2)} ms` : '—', has(st.build) ? 'measured' : 'none'],
          ['Detect → send (live)', fmtMs(send), has(send) ? 'live' : 'none'],
        ]}
      />
      <LatencyCard
        stage="3 · Landing"
        value={has(l.landing.feasibleOffsetSlots) ? `S+${l.landing.feasibleOffsetSlots!.p50!.toFixed(0)}` : `S+${l.landing.modeledEntrySlot} (model)`}
        caption="Earliest slot a buy could land in: the slot in progress when a send leaving at send-ready (+ one-way) reaches the leader"
        source={has(l.landing.feasibleOffsetSlots) ? 'measured' : 'assumption'}
        rows={[
          ['Feasible landing (slots after migrate)', fmtSlots(l.landing.feasibleOffsetSlots), has(l.landing.feasibleOffsetSlots) ? 'measured' : 'none'],
          ['Migrate slot still open on arrival', fmtPct(l.landing.migrateSlotStillOpenShare ?? null), l.landing.migrateSlotStillOpenShare == null ? 'none' : 'measured'],
          ['First outside buy lands', fmtSlots(first), has(first) ? 'proxy' : 'none'],
          ['Outside buy inside migrate slot', fmtPct(l.landing.sameSlotBuyShare), l.landing.sameSlotBuyShare === null ? 'none' : 'proxy'],
          ['S+1 on-chain fill rate', fmtPct(l.landing.onChainFillRate), l.landing.onChainFillRate === null ? 'none' : 'proxy'],
          ['Trader send → land', fmtSlots(l.landing.liveEntryLandSlots), has(l.landing.liveEntryLandSlots) ? 'live' : 'none'],
          ['Trader send → confirm seen', fmtMs(l.landing.liveEntryConfirmMs), has(l.landing.liveEntryConfirmMs) ? 'live' : 'none'],
        ]}
      />
      <LatencyCard
        stage="4 · Exit"
        value={has(exitLive) ? `${exitLive.p50!.toFixed(0)} ms` : (l.exit.horizonsFromStream ?? 0) > 0 ? 'next slot' : l.exit.assumedExitLandMs === null ? '—' : `+${l.exit.assumedExitLandMs} ms`}
        caption={
          has(exitLive)
            ? 'Exit send → landed (live trader)'
            : 'Paper sells decide on the stream state at the horizon and fill on the first state in a later slot — no RPC'
        }
        source={has(exitLive) ? 'live' : (l.exit.horizonsFromStream ?? 0) > 0 ? 'measured' : 'assumption'}
        rows={[
          ['Horizons from stream / RPC fallback', `${l.exit.horizonsFromStream ?? 0} / ${l.exit.horizonsFromRpc ?? 0}`, (l.exit.horizonsFromStream ?? 0) > 0 ? 'measured' : 'none'],
          ['Next-slot fill / unchanged pool', `${l.exit.horizonNextSlotFills ?? 0} / ${l.exit.horizonUnchangedFills ?? 0}`, (l.exit.horizonsFromStream ?? 0) > 0 ? 'measured' : 'none'],
          ['Trigger → send (live)', fmtMs(l.exit.liveExitTriggerToSendMs), has(l.exit.liveExitTriggerToSendMs) ? 'live' : 'none'],
          ['Send → landed (live)', fmtMs(exitLive), has(exitLive) ? 'live' : 'none'],
          ['Path rules on an unchanged pool', fmtPct(l.exit.unchangedFillShare), l.exit.unchangedFillShare === null ? 'none' : 'measured'],
        ]}
      />
    </section>
  );
}

function RacerWorkspace({
  summary,
  migrations,
  paper,
  onUnauthorized,
}: {
  summary: RacerSummary | null;
  migrations: RacerMigrationRow[];
  paper: RacerPaperRow[];
  onUnauthorized: () => void;
}) {
  if (!summary || !summary.curves) {
    return (
      <section class="table-zone">
        <Panel title="Migration racer">
          <div className="empty">Racer worker has not reported yet</div>
        </Panel>
      </section>
    );
  }
  const { curves, migrations: m, slots, competition, bundles } = summary;
  const horizons = summary.paper.horizons;
  const last = horizons[horizons.length - 1] ?? null;
  const lastSlot1 = last?.entries?.[HEADLINE_KEY] ?? null;
  const excluded = Object.entries(summary.paper.excludedReasons ?? {});
  const coverage = Object.entries(m.coverage ?? {}).sort((a, b) => b[1] - a[1]);
  const coverageTotal = coverage.reduce((n, [, v]) => n + v, 0);
  const verifiedDone = summary.paper.evaluation?.verifiedCompleted ?? 0;
  // Cohort × hold pivot: one row per cohort, a column per hold.
  const cohortRows = new Map<string, { label: string; byHold: Map<number, { n: number; mean: number | null }> }>();
  for (const h of horizons) {
    const list = [
      { id: 'below', label: `Depletion < ${summary.paper.highDepletionThresholdPct ?? 90}%`, stats: h.cohorts?.belowHighDepletion },
      { id: 'high', label: `Depletion ≥ ${summary.paper.highDepletionThresholdPct ?? 90}%`, stats: h.cohorts?.highDepletion },
      { id: 'unknown', label: 'Depletion unknown', stats: h.cohorts?.unknownDepletion },
      ...(h.cohorts?.byCaller ?? []).map((c) => ({ id: `caller:${c.caller}`, label: `Caller ${c.caller.slice(0, 6)}…`, stats: c.stats })),
    ];
    for (const c of list) {
      if (!c.stats || c.stats.n === 0) continue;
      const row = cohortRows.get(c.id) ?? { label: c.label, byHold: new Map() };
      row.byHold.set(h.sec, { n: c.stats.n, mean: c.stats.mean });
      cohortRows.set(c.id, row);
    }
  }
  const cohortList = [...cohortRows.values()].sort(
    (a, b) => Math.max(...[...b.byHold.values()].map((x) => x.n)) - Math.max(...[...a.byHold.values()].map((x) => x.n)),
  );
  return (
    <div class="racer-stack">
      <div class="track-strip dry-run-strip" role="status">
        Measurement only — no wallet, nothing sent. {verifiedDone}/300 verified +1 mid tracks before any edge decision ·{' '}
        {summary.paper.verifiedTracks ?? 0} verified of {summary.paper.tracks} tracks
        {excluded.length ? ` (excluded: ${excluded.map(([r, n]) => `${r} ${n}`).join(', ')})` : ''}. “Reach” is idealised; realistic legs
        pay fees + tips, sell where the sell lands and count rugs as −100%.
      </div>

      <Panel title="Latency — detection → sending → landing → exit">
        <RacerLatencyPanel summary={summary} />
      </Panel>

      <section class="metric-grid metric-grid-wide" aria-label="Racer headline">
        <KpiCard
          label={last ? `Verified ${last.sec}s +1 mid median (net)` : 'Verified +1 mid median'}
          value={lastSlot1 ? fmtSignedPct(lastSlot1.median) : '—'}
          detail={lastSlot1 ? `win ${fmtPct(lastSlot1.winRate)} · n=${lastSlot1.n} · reach ${fmtSignedPct(last!.medianReachPct)}` : 'no verified tracks yet'}
          tone={pnlTone(lastSlot1?.median ?? null)}
        />
        <KpiCard
          label="Migrations seen"
          value={String(m.total)}
          detail={`${m.tracked} tracked · ${m.fired} fired · last ${summary.lastMigrateAt ? formatTime(summary.lastMigrateAt) : '—'}`}
        />
        <KpiCard
          label="Complete → migrate"
          value={slots.completeToMigrate.p50 === null ? '—' : `${slots.completeToMigrate.p50.toFixed(1)} slots`}
          detail={`p90 ${slots.completeToMigrate.p90 === null ? '—' : slots.completeToMigrate.p90.toFixed(1)} · n=${slots.completeToMigrate.n}`}
        />
        <KpiCard
          label="Fire → complete"
          value={slots.fireToComplete.p50 === null ? '—' : `${slots.fireToComplete.p50.toFixed(1)} slots`}
          detail={`p90 ${slots.fireToComplete.p90 === null ? '—' : slots.fireToComplete.p90.toFixed(1)} · n=${slots.fireToComplete.n}`}
        />
        <KpiCard
          label="Lost attempts / migration"
          value={competition.failedPerMigration === null ? '—' : competition.failedPerMigration.toFixed(2)}
          detail={`contested ${fmtPct(competition.contestedShare)} · ${competition.attempts} attempts`}
        />
        <KpiCard
          label="Top caller share"
          value={summary.topCallers[0] ? fmtPct(summary.topCallers[0].share) : '—'}
          detail={summary.topCallers[0] ? `${summary.topCallers[0].caller.slice(0, 6)}… · ${summary.topCallers[0].wins} wins` : 'no wins yet'}
        />
      </section>

      <Panel
        title="Verified paper performance (+1 mid, 0.1 SOL per trade)"
        action={
          <ExportLink href="/api/reports/racer-summary.json" onUnauthorized={onUnauthorized}>
            Summary JSON
          </ExportLink>
        }
      >
        {horizons.length === 0 ? (
          <div className="empty">No completed paper tracks yet (horizons finish 5 min after a migration)</div>
        ) : (
          <div class="racer-two">
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Hold</th><th>n</th><th>Mean</th><th>Median</th><th>Win</th><th>95% CI of mean</th><th>Mean ex best</th><th>Paper P&amp;L</th></tr>
                </thead>
                <tbody>
                  {horizons.map((h) => (
                    <tr key={h.sec}>
                      <td>{h.sec}s</td>
                      <td>{h.entries?.[HEADLINE_KEY]?.n ?? 0}</td>
                      <PnlCell v={h.headline?.mean ?? null} />
                      <PnlCell v={h.entries?.[HEADLINE_KEY]?.median ?? null} />
                      <td>{fmtPct(h.entries?.[HEADLINE_KEY]?.winRate ?? null)}</td>
                      <td>{h.headline?.ciLow == null || h.headline.ciHigh == null ? '—' : `${fmtSignedPct(h.headline.ciLow)} to ${fmtSignedPct(h.headline.ciHigh)}`}</td>
                      <PnlCell v={h.headline?.meanWithoutBest ?? null} />
                      <td>{h.headline ? `${h.headline.cumulativeFixedSizePnlSol >= 0 ? '+' : ''}${h.headline.cumulativeFixedSizePnlSol.toFixed(4)} SOL` : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Median by entry</th><th>Open</th><th>Reach</th>
                    {REALISTIC_KEYS.map((k) => <th key={k}>{REALISTIC_LABELS[k]}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {horizons.map((h) => (
                    <tr key={h.sec}>
                      <td>{h.sec}s</td>
                      <PnlCell v={h.medianOpenPct} />
                      <PnlCell v={h.medianReachPct} />
                      {REALISTIC_KEYS.map((k) => <PnlCell key={k} v={h.entries?.[k]?.median ?? null} />)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
        <p class="panel-note">Paper P&amp;L is fixed 0.1 SOL per scored trade — not a wallet balance or an executable equity curve.</p>
      </Panel>

      <Panel title={`Cumulative paper P&L — ${last?.sec ?? 300}s +1 mid, 0.1 SOL/trade`}>
        {!last?.headline?.fixedSizePnlCurve?.length ? (
          <div className="empty">No verified completed exits yet</div>
        ) : (
          <div class="racer-chart">
            <PnlChart
              datasets={[{
                label: `${last.sec}s · 0.1 SOL/trade`,
                color: '#3f8f74',
                points: last.headline.fixedSizePnlCurve.map((p) => ({ time: p.exitAt, mint: p.mint, pnlSol: p.pnlSol, cumulativePnlSol: p.cumulativePnlSol })),
              }]}
              labelFormat={formatTime}
            />
          </div>
        )}
        <p class="panel-note">Chronological exits (unavailable reprices: {last?.headline?.fixedSizeCurveUnavailable ?? 0}); no compounding, landing failures or wallet constraints.</p>
      </Panel>

      <Panel title="Predeclared path exits vs same-trade 300 s exit">
        {!summary.paper.pathRules?.some((x) => x.n > 0) ? (
          <div className="empty">No verified complete vault paths yet</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Rule</th><th>n</th><th>Mean</th><th>Median</th><th>Win</th><th>95% CI</th><th>Same-trade 300 s mean</th><th>Unchanged-pool fills</th><th>Unpriced</th><th>No path</th></tr>
              </thead>
              <tbody>
                {summary.paper.pathRules.map((x) => (
                  <tr key={x.rule}>
                    <td>{x.rule}</td><td>{x.n}</td>
                    <PnlCell v={x.result.mean} /><PnlCell v={x.result.median} /><td>{fmtPct(x.result.winRate)}</td>
                    <td>{x.result.ciLow == null || x.result.ciHigh == null ? '—' : `${fmtSignedPct(x.result.ciLow)} to ${fmtSignedPct(x.result.ciHigh)}`}</td>
                    <PnlCell v={x.sameTradeTimeBaseline.mean} />
                    <td>{x.unchangedFills ?? 0}</td><td>{x.unpriced}</td><td>{x.notCaptured ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p class="panel-note">
          Exploratory set = first 300 verified tracks; later tracks are reserved. Fills use the next observed pool state, or the unchanged last state when nothing traded on a
          continuous stream; a stream gap leaves the exit unpriced. No rule is validated for live use.
        </p>
      </Panel>

      <div class="racer-two">
        <Panel title="Migration coverage">
          {coverage.length === 0 ? <div className="empty">No migrations yet</div> : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Outcome</th><th>Migrations</th><th>Share</th></tr></thead>
                <tbody>
                  {coverage.map(([k, v]) => (
                    <tr key={k}><td>{k}</td><td>{v}</td><td>{fmtPct(coverageTotal ? v / coverageTotal : null)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p class="panel-note">Every migration reconciles to a verified track or a named exclusion. Rugged tracks scored −100%: {summary.paper.ruggedTracks ?? 0}.</p>
        </Panel>
        <Panel title="Execution &amp; capital (proxy)">
          <KvTable
            rows={[
              ['S+1 buys / on-chain failures', `${summary.execution?.slot1BuyAttempts ?? 0} / ${summary.execution?.slot1Failed ?? 0}`],
              ['S+1 on-chain fill proxy', fmtPct(summary.execution?.observedOnChainFillRate ?? null)],
              ['Buy volume S / S+1', `${summary.execution?.slot0BuyVolumeSol?.toFixed(2) ?? '—'} / ${summary.execution?.slot1BuyVolumeSol?.toFixed(2) ?? '—'} SOL`],
              ['Distinct buyers S / S+1', `${summary.execution?.slot0DistinctBuyerOwners ?? 0} / ${summary.execution?.slot1DistinctBuyerOwners ?? 0}`],
              ['Failed-buy network fee (mean)', summary.execution?.meanFailedNetworkFeeSol == null ? '—' : `${summary.execution.meanFailedNetworkFeeSol.toFixed(6)} SOL`],
              ['Live cost / round trip (measured share)', `${summary.paper.medianLiveCostSol == null ? '—' : `${summary.paper.medianLiveCostSol.toFixed(4)} SOL`} (${fmtPct(summary.paper.measuredCostShare ?? null)})`],
              ['Size scenario', last?.sizeScenario ? `${last.sizeScenario.sizeSol} SOL · ${last.sizeScenario.priced} priced / ${last.sizeScenario.unavailable} unavailable` : '—'],
              ['Scenario paper P&L / fill-adjusted', last?.sizeScenario ? `${last.sizeScenario.pnlSol.toFixed(4)} / ${last.sizeScenario.onChainFillAdjustedPnlSol == null ? '—' : last.sizeScenario.onChainFillAdjustedPnlSol.toFixed(4)} SOL` : '—'],
              ['Peak capital / ATA rent locked', last?.sizeScenario ? `${last.sizeScenario.peakConcurrentSol.toFixed(4)} / ${last.sizeScenario.ataRentLockedSol.toFixed(4)} SOL` : '—'],
            ]}
          />
          <p class="panel-note">S+1 flow is diagnostic only. Failed buys are no position; off-chain drops are unobservable, so the fill rate is a proxy, not our landing rate.</p>
        </Panel>
      </div>

      <Panel title={`Cohorts — +1 mid mean by hold (n)`}>
        {cohortList.length === 0 ? <div className="empty">Verified paper tracks will populate these cohorts</div> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Cohort</th>{horizons.map((h) => <th key={h.sec}>{h.sec}s</th>)}</tr></thead>
              <tbody>
                {cohortList.map((row) => (
                  <tr key={row.label}>
                    <td>{row.label}</td>
                    {horizons.map((h) => {
                      const cell = row.byHold.get(h.sec);
                      return (
                        <td key={h.sec} class={cell?.mean == null ? '' : cell.mean >= 0 ? 'profit-text' : 'loss-text'}>
                          {cell ? `${fmtSignedPct(cell.mean)} (${cell.n})` : '—'}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p class="panel-note">Depletion is the base-vault drain after the migrate tx; it does not show who bought. Small-n cohorts are anecdotes.</p>
      </Panel>

      <Panel title="Decision-time feature splits (+1 mid, longest hold)">
        {!last?.featureSplits?.length ? <div className="empty">No verified feature snapshots yet</div> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Feature</th><th>Cohort</th><th>n</th><th>Mean</th><th>Median</th><th>Win</th><th>95% CI</th></tr></thead>
              <tbody>
                {last.featureSplits.map((x, i) => (
                  <tr key={`${x.feature}-${x.cohort}`}>
                    <td>{i > 0 && last.featureSplits![i - 1]!.feature === x.feature ? '' : x.feature}</td>
                    <td title={x.cohort}>{x.cohort.length > 18 ? `${x.cohort.slice(0, 8)}…${x.cohort.slice(-4)}` : x.cohort}</td>
                    <td>{x.stats.n}</td>
                    <PnlCell v={x.stats.mean} /><PnlCell v={x.stats.median} /><td>{fmtPct(x.stats.winRate)}</td>
                    <td>{x.stats.ciLow === null || x.stats.ciHigh === null ? '—' : `${fmtSignedPct(x.stats.ciLow)} to ${fmtSignedPct(x.stats.ciHigh)}`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p class="panel-note">
          Exploratory rows {summary.paper.evaluation?.exploratoryRows ?? 0} / reserved {summary.paper.evaluation?.reservedLaterRows ?? 0}. Inputs are what was observable when the migrate reached the worker.
        </p>
      </Panel>

      <div class="racer-two">
        <Panel title="Race">
          <KvTable
            rows={[
              ['Curves admitted / fired / open', `${curves.admitted} / ${curves.fired} / ${curves.open}`],
              ['Outcomes: migrated / no-pool / expired', `${curves.migrated} / ${curves.completeNoPool} / ${curves.expired}`],
              ['Fire → migrate', fmtDist(slots.fireToMigrate)],
              ['Bundled completion / pool buy', `${fmtPct(bundles.curveBuyBeforeShare)} / ${fmtPct(bundles.poolBuyAfterShare)}`],
              ['Last curve write', summary.lastCurveAt ? formatTime(summary.lastCurveAt) : '—'],
            ]}
          />
        </Panel>
        <Panel title="Top migrate callers">
          {summary.topCallers.length === 0 ? <div className="empty">No migrations yet</div> : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Caller</th><th>Wins</th><th>Share</th></tr></thead>
                <tbody>
                  {summary.topCallers.map((c) => (
                    <tr key={c.caller}>
                      <td title={c.caller}>{`${c.caller.slice(0, 6)}…${c.caller.slice(-4)}`}</td>
                      <td>{c.wins}</td>
                      <td>{fmtPct(c.share)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
      <section class="table-zone analytics-zone">
        <Panel
          title="Recent migrations"
          action={
            <ExportLink href="/api/reports/racer-migrations.csv" onUnauthorized={onUnauthorized}>
              Migrations CSV
            </ExportLink>
          }
        >
          {migrations.length === 0 ? (
            <div className="empty">No migrations recorded yet</div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Mint</th>
                    <th>Caller</th>
                    <th>Tracked</th>
                    <th>Fire→done</th>
                    <th>Done→mig</th>
                    <th>Lost</th>
                    <th>Bundle</th>
                    <th>Reach price</th>
                    <th>Migrate slot</th>
                    <th>Receipt / feasible</th>
                    <th>Send-ready</th>
                    <th>Depth open / observed / gate</th>
                    <th>Exclusion</th>
                  </tr>
                </thead>
                <tbody>
                  {migrations.map((row) => (
                    <tr key={row.signature}>
                      <td>{formatTime(row.at)}</td>
                      <td title={row.mint}>{`${row.mint.slice(0, 6)}…`}</td>
                      <td title={row.caller ?? ''}>{row.caller ? `${row.caller.slice(0, 4)}…` : '—'}</td>
                      <td>{row.tracked ? 'yes' : 'no'}</td>
                      <td>{row.fireToComplete ?? '—'}</td>
                      <td>{row.completeToMigrate ?? '—'}</td>
                      <td>{row.failedAttempts}</td>
                      <td>{[row.curveBuyBefore ? 'curve' : null, row.poolBuyAfter ? 'pool' : null].filter(Boolean).join('+') || '—'}</td>
                      <td>{row.reachPrice === null ? 'unpriced' : row.reachPrice.toExponential(2)}</td>
                      <td>{row.migrateSlot ?? '—'}</td>
                      <td>
                        {row.migrateSlot == null
                          ? '—'
                          : `${row.receiptSlot == null ? '—' : `S${row.receiptSlot - row.migrateSlot >= 0 ? '+' : ''}${row.receiptSlot - row.migrateSlot}`} / ${row.feasibleSlot == null ? '—' : `${row.feasibleSlot} (S+${row.feasibleSlot - row.migrateSlot})`}`}
                      </td>
                      <td>{row.sendReadyUs == null ? '—' : `${(row.sendReadyUs / 1000).toFixed(2)} ms`}</td>
                      <td>{[row.initialQuoteDepthSol, row.observedQuoteDepthSol, row.depthThresholdSol].map((v) => v == null ? '—' : `${v.toFixed(2)} SOL`).join(' / ')}</td>
                      <td>{row.unpricedReason ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </section>

      <section class="table-zone analytics-zone">
        <Panel
          title="Paper tracks (0.1 SOL, fees + impact)"
          action={
            <ExportLink href="/api/reports/racer-paper.csv" onUnauthorized={onUnauthorized}>
              Paper CSV
            </ExportLink>
          }
        >
          {paper.length === 0 ? (
            <div className="empty">No paper tracks yet</div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Mint</th>
                    <th>Armed</th>
                    <th>Entry quality</th>
                    <th>Same-slot depletion</th>
                    {paper[0]!.horizons.map((h) => (
                      <th key={h.sec}>{`${h.sec}s reach / +1 mid`}</th>
                    ))}
                    <th>Live cost</th>
                    <th>Entry depth</th>
                  </tr>
                </thead>
                <tbody>
                  {paper.map((row) => (
                    <tr key={row.mint}>
                      <td>{formatTime(row.at)}</td>
                      <td title={row.mint}>{`${row.mint.slice(0, 6)}…`}</td>
                      <td>{row.armed === true ? 'early' : row.armed === false ? 'late' : '—'}</td>
                      <td>{row.entryQuality ?? 'legacy-unverified'}</td>
                      <td>{row.sameSlotDepletionPct == null ? '—' : `${row.sameSlotDepletionPct.toFixed(1)}%`}</td>
                      {row.horizons.map((h) => {
                        const s1 = h.pnl?.[HEADLINE_KEY] ?? null;
                        if (h.rugged) return <td key={h.sec} class="loss-text">rugged −100%</td>;
                        return (
                          <td key={h.sec}>
                            <span class={pnlTone(h.pnlReachPct) === 'loss' ? 'loss-text' : 'profit-text'}>{fmtSignedPct(h.pnlReachPct)}</span>
                            {' / '}
                            <span class={pnlTone(s1) === 'loss' ? 'loss-text' : s1 === null ? '' : 'profit-text'}>{fmtSignedPct(s1)}</span>
                          </td>
                        );
                      })}
                      <td>
                        {row.liveCostSol == null ? '—' : `${row.liveCostSol.toFixed(4)}${row.liveCostSource === 'measured' ? '' : ' (default)'}`}
                      </td>
                      <td>{row.horizons[0]?.quoteSol == null ? '—' : `${row.horizons[0].quoteSol.toFixed(0)} SOL @${row.horizons[0].sec}s`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </section>
    </div>
  );
}

function BreakerList({ rows }: { rows: BreakerRow[] }) {
  if (rows.length === 0) return <div class="empty">No breaker events yet</div>;
  return (
    <ol class="events compact">
      {rows.map((row, i) => (
        <li class={row.tripped ? 'error' : 'info'} key={`${row.type}-${row.at}-${i}`}>
          <time>{formatTime(row.at)}</time>
          <div>
            <strong>{row.type}</strong>
            <span>{row.detail ?? (row.tripped ? 'tripped' : 'cleared')}</span>
          </div>
        </li>
      ))}
    </ol>
  );
}

function VetoReasonsPanel({
  breakdown,
  funnel,
}: {
  breakdown: VetoBreakdown | null;
  funnel: FunnelStats | null;
}) {
  if (!breakdown || breakdown.totalVetoed === 0) {
    return <div class="empty">No vetoes recorded in this window</div>;
  }
  // Split reliability (recoverable) from structural rejections so it's obvious
  // whether the funnel is narrow because of missing data or real risk signals.
  const unknownPct = breakdown.primary
    .filter((r) => r.category === 'unknown')
    .reduce((sum, r) => sum + r.pct, 0);
  const catLabel: Record<VetoReasonRow['category'], string> = {
    unknown: 'reliability',
    hard_fail: 'structural',
    low_score: 'soft gate',
    none: '—',
  };
  return (
    <div>
      <div class="veto-summary">
        <span>
          Accept rate <strong>{funnel ? `${funnel.acceptRatePct.toFixed(2)}%` : '—'}</strong>
        </span>
        <span>
          Vetoed <strong>{breakdown.totalVetoed}</strong>
        </span>
        <span>
          Recoverable (unknowns) <strong>{unknownPct.toFixed(0)}%</strong>
        </span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Primary reason</th>
              <th>Type</th>
              <th>Count</th>
              <th>% of vetoed</th>
            </tr>
          </thead>
          <tbody>
            {breakdown.primary.map((row) => (
              <tr key={row.reason}>
                <td class="mono">{row.reason}</td>
                <td><span class={`pill veto-${row.category}`}>{catLabel[row.category]}</span></td>
                <td>{row.count}</td>
                <td>{row.pct.toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function VetoDryRunPanel({
  comparison,
  rows,
  onSelect,
}: {
  comparison: VetoDryRunComparison | null;
  rows: ShadowOutcomeRow[];
  onSelect: (row: ShadowOutcomeRow) => void;
}) {
  const live = comparison?.liveAccepted;
  const tracked = comparison?.trackedN ?? rows.length;
  const exits = comparison?.exitSimN ?? rows.filter((row) => row.netPnlSol !== null).length;
  const legacy = comparison?.legacyPeakN ?? rows.filter((row) => row.outcomeVersion === null).length;

  return (
    <div class="panel-body">
      <div class="veto-summary">
        <span>
          Live accepted <strong>{live?.n ?? 0}</strong>
        </span>
        <span>
          Live win <strong>{live ? `${live.winRatePct.toFixed(0)}%` : '—'}</strong>
        </span>
        <span>
          Live net{' '}
          <strong class={(live?.netPnlSol ?? 0) >= 0 ? 'profit-text' : 'loss-text'}>
            {formatSol(live?.netPnlSol)}
          </strong>
        </span>
        <span>
          Live exp <strong>{formatSol(live?.expectancySol)}</strong>
        </span>
        <span>
          Tracked <strong>{tracked}</strong>
        </span>
        <span>
          Exit sims <strong>{exits}</strong>
        </span>
        <span>
          Legacy peaks <strong>{legacy}</strong>
        </span>
        <span class="panel-note-inline">Counterfactual · not live capital</span>
      </div>
      {rows.length === 0 ? (
        <div class="empty">
          No veto dry-runs in this window yet (shadow needs the tracking window to finish).
        </div>
      ) : (
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Mint</th>
                <th>Veto</th>
                <th>Size</th>
                <th>Entry</th>
                <th>PnL</th>
                <th>Exit</th>
                <th>MFE</th>
                <th>Time</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.id}
                  class="position-row"
                  role="button"
                  tabIndex={0}
                  onClick={() => onSelect(row)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      onSelect(row);
                    }
                  }}
                >
                  <td class="mono">{short(row.mint)}</td>
                  <td>
                    <span class="pill veto">{row.primaryVetoCode ?? row.verdict}</span>
                  </td>
                  <td>{row.sizeSol != null ? `${zeroNumber(row.sizeSol).toFixed(3)} SOL` : '—'}</td>
                  <td>{formatPrice(row.baselinePrice)}</td>
                  <td class={zeroNumber(row.netPnlSol) >= 0 ? 'profit-text' : 'loss-text'}>
                    {formatSol(row.netPnlSol)}
                    {row.pnlPct != null ? ` (${zeroNumber(row.pnlPct).toFixed(1)}%)` : ''}
                  </td>
                  <td>{row.exitReason ?? '—'}</td>
                  <td>
                    {row.peakMfePct != null ? `${zeroNumber(row.peakMfePct).toFixed(1)}%` : '—'}
                    {row.hit50 ? <span class="pill accept">hit50</span> : null}
                  </td>
                  <td>{formatTime(row.createdAt)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ShadowOutcomeModal({ outcome, onClose }: { outcome: ShadowOutcomeRow; onClose: () => void }) {
  const pumpUrl = pumpFunUrl(outcome.mint);
  return (
    <div
      class="modal-backdrop"
      role="presentation"
      onClick={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <section class="position-modal" role="dialog" aria-modal="true" aria-labelledby="shadow-modal-title">
        <header class="modal-head">
          <div>
            <span>Veto dry-run details</span>
            <h2 id="shadow-modal-title">{short(outcome.mint)}</h2>
          </div>
          <button type="button" class="modal-close" aria-label="Close dry-run details" onClick={onClose}>
            X
          </button>
        </header>

        <div class="mint-panel">
          <span>Full mint · counterfactual (not live capital)</span>
          <strong class="mono breakable">{outcome.mint}</strong>
          <PumpFunLink href={pumpUrl} />
        </div>

        <div class="detail-grid">
          <DetailRow label="Primary veto" value={outcome.primaryVetoCode ?? '—'} />
          <DetailRow
            label="All veto codes"
            value={outcome.vetoCodes.length > 0 ? outcome.vetoCodes.join(', ') : '—'}
          />
          <DetailRow label="Verdict" value={outcome.verdict} />
          <DetailRow label="Size" value={outcome.sizeSol != null ? `${zeroNumber(outcome.sizeSol).toFixed(3)} SOL` : '—'} />
          <DetailRow label="Baseline (entry)" value={formatPrice(outcome.baselinePrice)} />
          <DetailRow label="Peak / trough" value={`${formatPrice(outcome.peakPrice)} / ${formatPrice(outcome.troughPrice)}`} />
          <DetailRow
            label="Net PnL"
            value={`${formatSol(outcome.netPnlSol)}${outcome.pnlPct != null ? ` (${zeroNumber(outcome.pnlPct).toFixed(1)}%)` : ''}`}
            tone={zeroNumber(outcome.netPnlSol) >= 0 ? 'profit' : 'loss'}
          />
          <DetailRow label="Gross / fees" value={`${formatSol(outcome.grossPnlSol)} / ${formatSol(outcome.feesSol)}`} />
          <DetailRow
            label="MFE / MAE"
            value={`${outcome.peakMfePct != null ? `${zeroNumber(outcome.peakMfePct).toFixed(1)}%` : '—'} / ${outcome.maxMaePct != null ? `${zeroNumber(outcome.maxMaePct).toFixed(1)}%` : '—'}`}
          />
          <DetailRow label="Hit +25 / +50" value={`${outcome.hit25 ? 'yes' : 'no'} / ${outcome.hit50 ? 'yes' : 'no'}`} />
          <DetailRow label="Exit reason" value={outcome.exitReason ?? '—'} />
          <DetailRow label="Hold" value={formatHoldMs(outcome.holdMs)} />
          <DetailRow label="Samples" value={String(outcome.samples)} />
          <DetailRow label="Tracked" value={formatHoldMs(outcome.trackedMs)} />
          <DetailRow label="Recorded" value={formatOptionalTime(outcome.createdAt)} />
        </div>
      </section>
    </div>
  );
}

function formatHoldMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return '—';
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`;
  if (ms < 3_600_000) return `${(ms / 60_000).toFixed(1)}m`;
  return `${(ms / 3_600_000).toFixed(1)}h`;
}

function EdgePanel({ edge }: { edge: EdgeAnalytics | null }) {
  if (!edge || edge.n === 0) return <div class="empty">No closed trades yet</div>;
  const ci = edge.expectancyPct;
  const pctText = (v: number | null | undefined, digits = 1) => (v === null || v === undefined || !Number.isFinite(v) ? '—' : `${v.toFixed(digits)}%`);
  const tone = (v: number | null | undefined) => (zeroNumber(v) >= 0 ? 'profit-text' : 'loss-text');
  const cohorts = [...edge.cohorts.byRisk, ...edge.cohorts.bySuffix, ...edge.cohorts.byExit];
  return (
    <div>
      <div class="veto-summary">
        <span>
          Expectancy <strong class={tone(ci?.point)}>{pctText(ci?.point, 2)}</strong>
          {ci && <> · 95% CI [{ci.lo.toFixed(2)}, {ci.hi.toFixed(2)}]</>}
        </span>
        <span>
          Net <strong class={tone(edge.netSol)}>{formatSol(edge.netSol)}</strong> over {edge.n}
        </span>
        <span>
          Win <strong>{pctText(edge.winRatePct, 0)}</strong>
        </span>
        <span>
          Fees <strong>{pctText(edge.feePctOfNotional, 2)}</strong> of notional
        </span>
        <span>
          Emergency share of losses <strong>{pctText(edge.emergencyShareOfLossesPct, 0)}</strong>
        </span>
        {edge.monitor && (
          <span>
            Monitor <strong class={edge.monitor.negative ? 'loss-text' : 'profit-text'}>{edge.monitor.negative ? 'NEGATIVE — paused' : 'ok'}</strong>
          </span>
        )}
      </div>
      <div class="veto-summary">
        {Object.entries(edge.latency).map(([kind, l]) => (
          <span key={kind}>
            {kind} p50/p95 <strong>{l.count ? `${Math.round(l.p50)} / ${Math.round(l.p95)} ms` : '—'}</strong>
          </span>
        ))}
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Cohort</th>
              <th>n</th>
              <th>Mean</th>
              <th>Win</th>
              <th>Net</th>
            </tr>
          </thead>
          <tbody>
            {cohorts.map((row) => (
              <tr key={row.cohort}>
                <td>{row.cohort}</td>
                <td>{row.n}</td>
                <td class={tone(row.meanPct)}>{pctText(row.meanPct, 2)}</td>
                <td>{pctText(row.winRatePct, 0)}</td>
                <td class={tone(row.netSol)}>{formatSol(row.netSol)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {edge.calibration.length > 0 && (
        <div class="veto-summary">
          {edge.calibration.map((b) => (
            <span key={b.bin}>
              p {b.bin}: <strong>{b.winRatePct.toFixed(0)}%</strong> won (n={b.n})
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function RelaxedRiskPanel({ analytics }: { analytics: RelaxedRiskAnalytics | null }) {
  if (!analytics) return <div class="empty">No relaxed-risk analytics yet</div>;
  const relaxed = analytics.groups.find((g) => g.kind === 'relaxed');
  return (
    <div>
      <div class="veto-summary">
        <span>
          Relaxed PnL <strong class={zeroNumber(relaxed?.netPnlSol) >= 0 ? 'profit-text' : 'loss-text'}>{formatSol(relaxed?.netPnlSol)}</strong>
        </span>
        <span>
          Emergencies <strong>{analytics.rollback.relaxedEmergencyExits}</strong>
        </span>
        <span>
          Failed entries <strong>{analytics.rollback.relaxedFailedEntries}</strong>
        </span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Group</th>
              <th>Accept</th>
              <th>Enter</th>
              <th>Closed</th>
              <th>Win</th>
              <th>Net</th>
              <th>MFE / MAE</th>
            </tr>
          </thead>
          <tbody>
            {analytics.groups.map((row) => (
              <tr key={row.kind}>
                <td><span class={`pill ${row.kind === 'relaxed' ? 'veto-low_score' : 'veto-none'}`}>{row.kind}</span></td>
                <td>{row.accepted}</td>
                <td>{row.entered}</td>
                <td>{row.closed}</td>
                <td>{row.winRatePct.toFixed(0)}%</td>
                <td class={row.netPnlSol >= 0 ? 'profit-text' : 'loss-text'}>{formatSol(row.netPnlSol)}</td>
                <td>{row.avgMfePct.toFixed(1)}% / {row.avgMaePct.toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div class="veto-summary">
        {analytics.h4Reasons.slice(0, 4).map((row) => (
          <span key={row.reason}>
            H4 {row.reason} <strong>{row.count}</strong>
          </span>
        ))}
      </div>
    </div>
  );
}

function PositionsTable({ rows, onSelect }: { rows: PositionRow[]; onSelect: (row: PositionRow) => void }) {
  if (rows.length === 0) return <div class="empty">No positions match this view</div>;
  return (
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Mint</th>
            <th>State</th>
            <th>Size</th>
            <th>Entry</th>
            <th>PnL</th>
            <th>Exit</th>
            <th>Time</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.id}
              class="position-row"
              role="button"
              tabIndex={0}
              onClick={() => onSelect(row)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  onSelect(row);
                }
              }}
            >
              <td class="mono">{short(row.mint)}</td>
              <td>
                <span class={`pill ${row.state.toLowerCase()}`}>{row.state}</span>
                {row.relaxedRisk && <span class="pill veto-low_score">relaxed</span>}
              </td>
              <td>{zeroNumber(row.sizeSol).toFixed(3)} SOL</td>
              <td>{formatPrice(row.entryPrice)}</td>
              <td class={zeroNumber(row.pnlSol) >= 0 ? 'profit-text' : 'loss-text'}>
                {formatSol(row.pnlSol)} ({zeroNumber(row.pnlPct).toFixed(1)}%)
              </td>
              <td>{row.exitReason ?? '-'}</td>
              <td>{formatTime(row.closedAt ?? row.openedAt ?? row.createdAt)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PositionModal({ position, onClose }: { position: PositionRow; onClose: () => void }) {
  const pumpUrl = pumpFunUrl(position.mint);

  return (
    <div
      class="modal-backdrop"
      role="presentation"
      onClick={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <section class="position-modal" role="dialog" aria-modal="true" aria-labelledby="position-modal-title">
        <header class="modal-head">
          <div>
            <span>Position details</span>
            <h2 id="position-modal-title">{short(position.mint)}</h2>
          </div>
          <button type="button" class="modal-close" aria-label="Close position details" onClick={onClose}>
            X
          </button>
        </header>

        <div class="mint-panel">
          <span>Full mint</span>
          <strong class="mono breakable">{position.mint}</strong>
          <PumpFunLink href={pumpUrl} />
        </div>

        <div class="detail-grid">
          <DetailRow label="State" value={position.state} />
          <DetailRow label="Size" value={`${zeroNumber(position.sizeSol).toFixed(3)} SOL`} />
          <DetailRow label="Entry price" value={formatPrice(position.entryPrice)} />
          <DetailRow label="Exit price" value={formatPrice(position.exitPrice)} />
          <DetailRow label="PnL" value={`${formatSol(position.netPnlSol ?? position.pnlSol)} (${zeroNumber(position.pnlPct).toFixed(1)}%)`} tone={zeroNumber(position.netPnlSol ?? position.pnlSol) >= 0 ? 'profit' : 'loss'} />
          <DetailRow label="Gross / fees" value={`${formatSol(position.grossPnlSol)} / ${formatSol(position.feesSol)}`} />
          <DetailRow label="Unrealized" value={position.unrealizedSol == null ? '-' : formatSol(position.unrealizedSol)} />
          <DetailRow label="MFE / MAE" value={`${zeroNumber(position.mfePct).toFixed(1)}% / ${zeroNumber(position.maePct).toFixed(1)}%`} />
          <DetailRow label="Hold" value={position.holdMs == null ? '-' : `${(zeroNumber(position.holdMs) / 1000).toFixed(1)}s`} />
          <DetailRow label="Exit latency" value={position.exitTriggerToConfirmMs == null ? '-' : `${zeroNumber(position.exitTriggerToConfirmMs).toFixed(0)} ms`} />
          <DetailRow label="Soft score / HV" value={`${position.entrySoftScore ?? '-'} / ${position.highVolatility == null ? '-' : position.highVolatility ? 'yes' : 'no'}`} />
          <DetailRow label="Feed / venue" value={`${position.feedSource ?? '-'} / ${position.venue ?? '-'}`} />
          <DetailRow label="Exit reason" value={position.exitReason ?? '-'} />
          <DetailRow label="Relaxed risk" value={position.relaxedRisk ? (position.relaxedReasons?.join(', ') || 'yes') : 'no'} />
          <DetailRow label="Opened" value={formatOptionalTime(position.openedAt)} />
          <DetailRow label="Closed" value={formatOptionalTime(position.closedAt)} />
          <DetailRow label="Created" value={formatOptionalTime(position.createdAt)} />
          {position.entryTx && <DetailRow label="Entry tx" value={position.entryTx} mono />}
          {position.exitTx && <DetailRow label="Exit tx" value={position.exitTx} mono />}
        </div>
      </section>
    </div>
  );
}

function EventModal({ event, onClose }: { event: OperatorEvent; onClose: () => void }) {
  return (
    <div
      class="modal-backdrop"
      role="presentation"
      onClick={(clickEvent) => {
        if (clickEvent.currentTarget === clickEvent.target) onClose();
      }}
    >
      <section class="position-modal" role="dialog" aria-modal="true" aria-labelledby="event-modal-title">
        <header class="modal-head">
          <div>
            <span>Event details</span>
            <h2 id="event-modal-title">{event.category.replaceAll('_', ' ')}</h2>
          </div>
          <button type="button" class="modal-close" aria-label="Close event details" onClick={onClose}>
            X
          </button>
        </header>

        {event.entityMint && (
          <div class="mint-panel">
            <span>Related mint</span>
            <strong class="mono breakable">{event.entityMint}</strong>
            <PumpFunLink href={pumpFunUrl(event.entityMint)} />
          </div>
        )}

        <div class="detail-grid">
          <DetailRow label="Category" value={event.category.replaceAll('_', ' ')} />
          <DetailRow label="Level" value={event.level} tone={event.level === 'error' ? 'loss' : undefined} />
          <DetailRow label="Created" value={formatOptionalTime(event.createdAt)} />
          <DetailRow label="Entity mint" value={event.entityMint ?? '-'} mono={event.entityMint !== null} />
          <DetailRow label="Message" value={event.message} />
        </div>

        {event.payload !== undefined && event.payload !== null && (
          <div class="payload-panel">
            <span>Payload</span>
            <pre>{formatPayload(event.payload)}</pre>
          </div>
        )}
      </section>
    </div>
  );
}

function CandidateModal({ candidate, onClose }: { candidate: CandidateRow; onClose: () => void }) {
  return (
    <div
      class="modal-backdrop"
      role="presentation"
      onClick={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <section class="position-modal" role="dialog" aria-modal="true" aria-labelledby="candidate-modal-title">
        <header class="modal-head">
          <div>
            <span>Guardrail details</span>
            <h2 id="candidate-modal-title">{short(candidate.mint)}</h2>
          </div>
          <button type="button" class="modal-close" aria-label="Close guardrail details" onClick={onClose}>
            X
          </button>
        </header>

        <div class="mint-panel">
          <span>Full mint</span>
          <strong class="mono breakable">{candidate.mint}</strong>
          <PumpFunLink href={pumpFunUrl(candidate.mint)} />
        </div>

        <div class="detail-grid">
          <DetailRow label="Verdict" value={candidate.verdict ?? 'pending'} />
          <DetailRow label="Soft score" value={zeroNumber(candidate.softScore).toFixed(0)} />
          <DetailRow label="High volatility" value={candidate.highVolatility ? 'yes' : 'no'} />
          <DetailRow label="Created" value={formatOptionalTime(candidate.createdAt)} />
          <DetailRow label="Veto reasons" value={candidate.vetoReasons.length > 0 ? candidate.vetoReasons.join(', ') : '-'} />
          <DetailRow label="Relaxed risk" value={candidate.relaxedRisk ? (candidate.relaxedReasons?.join(', ') || 'yes') : 'no'} />
        </div>
        {candidate.hardChecks && candidate.hardChecks.length > 0 && (
          <div class="payload-panel">
            <span>Hard checks</span>
            <div class="check-chips">
              {candidate.hardChecks.map((check) => (
                <span class={`check-chip ${check.status}`} key={check.id} title={check.detail ?? check.label ?? check.id}>
                  {check.id}:{check.status}{check.reason ? `/${check.reason}` : ''}
                </span>
              ))}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

function DetailRow(props: { label: string; value: string; tone?: 'profit' | 'loss' | undefined; mono?: boolean }) {
  const toneClass = props.tone === 'profit' ? 'profit-text' : props.tone === 'loss' ? 'loss-text' : '';
  return (
    <div class="detail-row">
      <span>{props.label}</span>
      <strong class={`${props.mono ? 'mono breakable' : ''} ${toneClass}`}>{props.value}</strong>
    </div>
  );
}

function PumpFunLink({ href }: { href: string }) {
  return (
    <a class="external-link" href={href} target="_blank" rel="noopener noreferrer" aria-label="Open on pump.fun in a new tab">
      <span>Open on pump.fun</span>
      <span class="new-tab-indicator" aria-hidden="true">&#x2197;</span>
    </a>
  );
}

function EventList({ rows, empty, compact, onSelect }: { rows: OperatorEvent[]; empty: string; compact?: boolean; onSelect: (row: OperatorEvent) => void }) {
  if (rows.length === 0) return <div class="empty">{empty}</div>;
  return (
    <ol class={`events ${compact ? 'compact' : ''}`}>
      {rows.map((event) => (
        <li
          class={`${event.level} event-row`}
          key={event.id}
          role="button"
          tabIndex={0}
          onClick={() => onSelect(event)}
          onKeyDown={(keyEvent) => {
            if (keyEvent.key === 'Enter' || keyEvent.key === ' ') {
              keyEvent.preventDefault();
              onSelect(event);
            }
          }}
        >
          <time>{formatTime(event.createdAt)}</time>
          <div>
            <strong>{event.category.replaceAll('_', ' ')}</strong>
            <span>{event.message}</span>
          </div>
        </li>
      ))}
    </ol>
  );
}

function Candidates({ rows, onSelect }: { rows: CandidateRow[]; onSelect: (row: CandidateRow) => void }) {
  if (rows.length === 0) return <div class="empty">No guardrail decisions yet</div>;
  return (
    <div class="candidates">
      {rows.map((row) => (
        <div
          class="candidate interactive"
          key={row.id}
          role="button"
          tabIndex={0}
          onClick={() => onSelect(row)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              onSelect(row);
            }
          }}
        >
          <div>
            <strong>{short(row.mint)}</strong>
            <span>{formatTime(row.createdAt)}</span>
          </div>
          <span class={`pill ${row.verdict ?? ''}`}>{row.verdict ?? 'pending'}</span>
          {row.relaxedRisk && <span class="pill veto-low_score">relaxed</span>}
          <b>{row.softScore?.toFixed(0) ?? '-'}</b>
        </div>
      ))}
    </div>
  );
}

function normalizeSummary(summary: Partial<Summary> | null | undefined): Summary {
  return {
    ...emptySummary,
    ...(summary ?? {}),
    mode: summary?.mode ?? emptySummary.mode,
    pnl: {
      realizedSol: zeroNumber(summary?.pnl?.realizedSol),
      realized24hSol: zeroNumber(summary?.pnl?.realized24hSol),
      realized7dSol: zeroNumber(summary?.pnl?.realized7dSol),
      winRatePct: zeroNumber(summary?.pnl?.winRatePct),
      closedCount: zeroNumber(summary?.pnl?.closedCount),
      wins: zeroNumber(summary?.pnl?.wins),
      losses: zeroNumber(summary?.pnl?.losses),
      expectancySol: zeroNumber(summary?.pnl?.expectancySol),
      profitFactor: zeroNumber(summary?.pnl?.profitFactor),
      maxDrawdownSol: zeroNumber(summary?.pnl?.maxDrawdownSol),
      currentDrawdownSol: zeroNumber(summary?.pnl?.currentDrawdownSol),
      avgWinSol: zeroNumber(summary?.pnl?.avgWinSol),
      avgLossSol: zeroNumber(summary?.pnl?.avgLossSol),
      feesSol: zeroNumber(summary?.pnl?.feesSol),
      unrealizedSol: zeroNumber(summary?.pnl?.unrealizedSol),
    },
    positions: {
      openCount: zeroNumber(summary?.positions?.openCount),
      openExposureSol: zeroNumber(summary?.positions?.openExposureSol),
      maxConcurrent: zeroNumber(summary?.positions?.maxConcurrent),
      pendingCount: zeroNumber(summary?.positions?.pendingCount),
      exitingCount: zeroNumber(summary?.positions?.exitingCount),
      failedCount: zeroNumber(summary?.positions?.failedCount),
    },
    flow: {
      graduations: zeroNumber(summary?.flow?.graduations),
      accepted: zeroNumber(summary?.flow?.accepted),
      vetoed: zeroNumber(summary?.flow?.vetoed),
      highVolatility: zeroNumber(summary?.flow?.highVolatility),
    },
    latency: {
      detection: {
        count: zeroNumber(summary?.latency?.detection?.count),
        p50: zeroNumber(summary?.latency?.detection?.p50),
        p95: zeroNumber(summary?.latency?.detection?.p95),
        max: zeroNumber(summary?.latency?.detection?.max),
      },
      exitConfirm: {
        count: zeroNumber(summary?.latency?.exitConfirm?.count),
        p50: zeroNumber(summary?.latency?.exitConfirm?.p50),
        p95: zeroNumber(summary?.latency?.exitConfirm?.p95),
        max: zeroNumber(summary?.latency?.exitConfirm?.max),
      },
      detectionSlots: normalizeLatency(summary?.latency?.detectionSlots),
      entryLandSlots: normalizeLatency(summary?.latency?.entryLandSlots),
      exitLandSlots: normalizeLatency(summary?.latency?.exitLandSlots),
    },
    system: {
      latestBreaker: summary?.system?.latestBreaker ?? null,
      latestEventAt: summary?.system?.latestEventAt ?? null,
      lastGraduationAt: summary?.system?.lastGraduationAt ?? null,
    },
  };
}

function normalizeShadowOutcome(row: ShadowOutcomeRow): ShadowOutcomeRow {
  return {
    id: zeroNumber(row.id),
    mint: row.mint ?? '',
    verdict: row.verdict ?? 'veto',
    primaryVetoCode: row.primaryVetoCode ?? null,
    vetoCodes: Array.isArray(row.vetoCodes) ? row.vetoCodes : [],
    baselinePrice: zeroNumber(row.baselinePrice),
    peakPrice: row.peakPrice == null ? null : zeroNumber(row.peakPrice),
    troughPrice: row.troughPrice == null ? null : zeroNumber(row.troughPrice),
    peakMfePct: row.peakMfePct == null ? null : zeroNumber(row.peakMfePct),
    maxMaePct: row.maxMaePct == null ? null : zeroNumber(row.maxMaePct),
    hit25: Boolean(row.hit25),
    hit50: Boolean(row.hit50),
    netPnlSol: row.netPnlSol == null ? null : zeroNumber(row.netPnlSol),
    grossPnlSol: row.grossPnlSol == null ? null : zeroNumber(row.grossPnlSol),
    feesSol: row.feesSol == null ? null : zeroNumber(row.feesSol),
    pnlPct: row.pnlPct == null ? null : zeroNumber(row.pnlPct),
    exitReason: row.exitReason ?? null,
    holdMs: row.holdMs == null ? null : zeroNumber(row.holdMs),
    sizeSol: row.sizeSol == null ? null : zeroNumber(row.sizeSol),
    samples: zeroNumber(row.samples),
    trackedMs: row.trackedMs == null ? null : zeroNumber(row.trackedMs),
    // Was dropped here, so VetoDryRunPanel's `outcomeVersion === null` check
    // counted every row as a legacy peak-only outcome.
    outcomeVersion: row.outcomeVersion ?? null,
    createdAt: row.createdAt ?? '',
  };
}

function normalizePosition(row: PositionRow): PositionRow {
  return {
    ...row,
    entryPrice: zeroNumber(row.entryPrice),
    sizeSol: zeroNumber(row.sizeSol),
    pnlSol: zeroNumber(row.pnlSol),
    pnlPct: zeroNumber(row.pnlPct),
  };
}

function normalizePnlPoint(point: PnlPoint): PnlPoint {
  return {
    ...point,
    pnlSol: zeroNumber(point.pnlSol),
    cumulativePnlSol: zeroNumber(point.cumulativePnlSol),
  };
}

function normalizeCandidate(row: CandidateRow): CandidateRow {
  return {
    ...row,
    softScore: zeroNumber(row.softScore),
  };
}

/**
 * Promise.all over an object. Keeps the ~14 dashboard fetches keyed by name so
 * adding or reordering one cannot silently swap two panels' data.
 */
async function allKeyed<T extends Record<string, Promise<unknown>>>(
  entries: T,
): Promise<{ [K in keyof T]: Awaited<T[K]> }> {
  const keys = Object.keys(entries) as Array<keyof T>;
  const values = await Promise.all(keys.map((k) => entries[k]));
  const out = {} as { [K in keyof T]: Awaited<T[K]> };
  keys.forEach((k, i) => {
    out[k] = values[i] as Awaited<T[typeof k]>;
  });
  return out;
}

/** Thrown for HTTP 401 so the app can pop the login instead of going offline. */
class AuthError extends Error {
  constructor(url: string) {
    super(`unauthorized: ${url}`);
    this.name = 'AuthError';
  }
}

const AUTH_STORAGE_KEY = 'pumpdesk-auth';

/** Memory first (works even when sessionStorage throws, e.g. private mode),
    sessionStorage second so a reload doesn't force a fresh login per tab. */
let memoryAuthToken: string | null = null;

function readAuthToken(): string | null {
  if (memoryAuthToken) return memoryAuthToken;
  try {
    return window.sessionStorage.getItem(AUTH_STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeAuthToken(token: string): void {
  memoryAuthToken = token;
  try {
    window.sessionStorage.setItem(AUTH_STORAGE_KEY, token);
  } catch {
    // Storage unavailable: the memory copy still covers this page load.
  }
}

function encodeBasicCredentials(username: string, password: string): string {
  const bytes = new TextEncoder().encode(`${username}:${password}`);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

/** fetch with the stored login credentials attached (no-op when auth is off
    and nobody has logged in). */
async function authedFetch(url: string, init?: RequestInit): Promise<Response> {
  const token = readAuthToken();
  const headers = new Headers(init?.headers);
  if (token && !headers.has('authorization')) headers.set('authorization', `Basic ${token}`);
  return fetch(url, { ...init, headers });
}

async function fetchJson<T>(url: string): Promise<T> {
  const res = await authedFetch(url);
  if (res.status === 401) throw new AuthError(url);
  if (!res.ok) throw new Error(`${url} ${res.status}`);
  return (await res.json()) as T;
}

/**
 * Minimal SSE reader over fetch. EventSource is not an option here: it cannot
 * send the Authorization header, so with dashboard auth on it would 401 on
 * every reconnect. Parses `event:`/`data:` blocks; `:heartbeat` comments are
 * ignored. Resolves cleanup via the returned unsubscribe; AbortError from the
 * caller's signal is swallowed, everything else goes to onError.
 */
function subscribeStream(options: {
  signal: AbortSignal;
  onReady: () => void;
  onEvent: (event: OperatorEvent) => void;
  onError: (error: unknown) => void;
}): () => void {
  let disposed = false;
  void (async () => {
    try {
      const res = await authedFetch('/api/stream', {
        signal: options.signal,
        headers: { accept: 'text/event-stream' },
      });
      if (res.status === 401) throw new AuthError('/api/stream');
      if (!res.ok || !res.body) throw new Error(`/api/stream ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let boundary = buffer.indexOf('\n\n');
        while (boundary >= 0) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          let name = '';
          const data: string[] = [];
          for (const line of block.split('\n')) {
            if (line.startsWith('event:')) name = line.slice('event:'.length).trim();
            else if (line.startsWith('data:')) data.push(line.slice('data:'.length).trimStart());
          }
          if (name === 'ready') options.onReady();
          else if (name === 'operator_event' && data.length > 0) {
            try {
              options.onEvent(JSON.parse(data.join('\n')) as OperatorEvent);
            } catch {
              // Malformed event: skip it, the next refresh still converges.
            }
          }
          boundary = buffer.indexOf('\n\n');
        }
      }
      throw new Error('/api/stream closed');
    } catch (error) {
      if (!disposed && (error as Error)?.name !== 'AbortError') options.onError(error);
    }
  })();
  return () => {
    disposed = true;
  };
}

function filenameFromDisposition(value: string | null): string | null {
  if (!value) return null;
  return /filename="([^"]+)"/.exec(value)?.[1] ?? null;
}

function zeroNumber(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function normalizeLatency(value: unknown): { count: number; p50: number; p95: number; max: number } {
  const v = (value ?? {}) as Record<string, unknown>;
  return { count: zeroNumber(v['count']), p50: zeroNumber(v['p50']), p95: zeroNumber(v['p95']), max: zeroNumber(v['max']) };
}

function formatSol(value: unknown): string {
  const number = zeroNumber(value);
  return `${number >= 0 ? '+' : ''}${number.toFixed(4)} SOL`;
}

function formatPrice(value: unknown): string {
  const number = zeroNumber(value);
  return number === 0 ? '0' : number.toPrecision(4);
}

function short(value: string): string {
  return value.length > 12 ? `${value.slice(0, 5)}...${value.slice(-5)}` : value;
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function formatOptionalTime(value: string | null): string {
  return value ? formatTime(value) : '-';
}

function pumpFunUrl(mint: string): string {
  return `https://pump.fun/coin/${encodeURIComponent(mint)}`;
}

function formatPayload(value: unknown): string {
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function formatMonthish(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

render(<App />, document.getElementById('app')!);
