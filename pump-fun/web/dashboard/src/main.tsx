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

/**
 * Which leg of the dual-track run the workspace is showing.
 *   live  — real capital
 *   dry   — the ideal paper twin opened alongside every accepted candidate
 *   delta — live minus dry: total execution drag
 */
type Track = 'live' | 'dry' | 'delta';

type DragCohort = 'both' | 'dry_only' | 'live_only';

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
  latency: { detection: emptyLatency, exitConfirm: emptyLatency },
  system: { latestBreaker: null, latestEventAt: null, lastGraduationAt: null },
};

function App() {
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
  const [breakers, setBreakers] = useState<BreakerRow[]>([]);
  const [drag, setDrag] = useState<ExecutionDrag>(emptyDrag);
  const [positionFilter, setPositionFilter] = useState<PositionFilter>('open');
  const [pnlRange, setPnlRange] = useState<PnlRange>('7d');
  const [vetoDryRunRange, setVetoDryRunRange] = useState<AnalyticsRange>('7d');
  const [track, setTrack] = useState<Track>('live');
  const [selectedDrag, setSelectedDrag] = useState<ExecutionDragRow | null>(null);
  const [status, setStatus] = useState<DashboardStatus>('connecting');
  const [streamReady, setStreamReady] = useState(false);
  const [manualRefreshing, setManualRefreshing] = useState(false);
  const [selectedPosition, setSelectedPosition] = useState<PositionRow | null>(null);
  const [selectedEvent, setSelectedEvent] = useState<OperatorEvent | null>(null);
  const [selectedCandidate, setSelectedCandidate] = useState<CandidateRow | null>(null);
  const [selectedShadow, setSelectedShadow] = useState<ShadowOutcomeRow | null>(null);

  const refresh = async () => {
    await fetchJson('/api/health');
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
    setBreakers(Array.isArray(res.breakers) ? res.breakers : []);
    setStatus('live');
    setStreamReady(true);
  };

  const handleManualRefresh = async () => {
    if (manualRefreshing) return;
    setManualRefreshing(true);
    try {
      await refresh();
    } catch {
      setStatus('offline');
      setStreamReady(false);
    } finally {
      setManualRefreshing(false);
    }
  };

  useEffect(() => {
    let disposed = false;
    const run = async () => {
      try {
        await refresh();
      } catch {
        if (!disposed) {
          setStatus('offline');
          setStreamReady(false);
        }
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
    if (!streamReady || status !== 'live') return;

    const source = new EventSource('/api/stream');
    source.addEventListener('operator_event', (event) => {
      const next = JSON.parse((event as MessageEvent).data) as OperatorEvent;
      setEvents((current) => [next, ...current].slice(0, 40));
      void refresh().catch(() => setStatus('offline'));
    });
    source.addEventListener('ready', () => setStatus('live'));
    source.onerror = () => {
      setStatus('reconnecting');
      setStreamReady(false);
      source.close();
    };
    return () => source.close();
    // `refresh` is captured in this closure, so every filter it reads must be a
    // dep. Without `track` an SSE-triggered refresh would refetch the PREVIOUS
    // track and clobber the one just selected. `vetoDryRunRange` was already
    // missing here for the same reason.
  }, [streamReady, status, positionFilter, pnlRange, vetoDryRunRange, track]);

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

  return (
    <div class="app-stage">
      <div class="dashboard-frame">
        <Sidebar />

        <main class="workspace">
          <header class="topbar">
            <div class="breadcrumb">
              <span>Dashboard</span>
              <b>/</b>
              <strong>Wallet PnL</strong>
            </div>
            <div class="account-strip">
              <Segmented value={track} values={['live', 'dry', 'delta']} labels={TRACK_LABELS} onChange={setTrack} />
              <div class="export-row">
                <a class="export-link" href={`/api/reports/trades.csv?range=7d&track=${track === 'delta' ? 'live' : track}`}>
                  Trades CSV
                </a>
                {track === 'delta' && (
                  <a class="export-link" href="/api/reports/execution-drag.csv?range=7d">Drag CSV</a>
                )}
                <a class="export-link" href="/api/reports/ops.json">Ops JSON</a>
                <a class="export-link" href="/api/reports/soak.json?range=7d">Soak JSON</a>
              </div>
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
              Dashboard API offline. Start the bot dashboard on 127.0.0.1:8787.
            </div>
          )}

          {/* Banner and KPI row share ONE grid row. `.workspace` has a fixed
              grid-template-rows, so a bare extra child would shift every
              section down a row and clip the metric grid. */}
          <div class="metrics-zone">
          {/* The one UX failure that would matter: a simulated number read as a
              wallet balance. Say so plainly whenever the track isn't live. */}
          {track !== 'live' && (
            <div class="track-strip" role="status">
              {track === 'dry'
                ? 'Dry-run track — an ideal paper twin of every accepted candidate. These are simulated fills, not wallet balances.'
                : 'Δ track — live minus dry-run. These are execution-drag differences, not wallet balances.'}
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
              detail={`n=${summary.latency.exitConfirm.count} · det p95 ${summary.latency.detection.p95.toFixed(0)} ms`}
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
              action={<Segmented value={pnlRange} values={['24h', '7d', '30d']} onChange={setPnlRange} />}
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
                        { label: 'Live', points: deltaSeries.livePoints, color: '#0877ad' },
                        { label: 'Dry-run', points: deltaSeries.dryPoints, color: '#0d7a59' },
                      ]
                    : [{ label: 'Realized PnL', points: pnl, color: finalPnl >= 0 ? '#0d7a59' : '#b94a48', fill: true }]
                }
              />
            </Panel>

            <Panel title="Risk & Ops">
              <RiskPanel risk={risk} summary={summary} funnel={funnel} />
            </Panel>
          </section>

          <section class="table-zone">
            {track === 'delta' ? (
              <Panel title="Execution drag per trade" action={<DragCoverageNote drag={drag} />}>
                <DragTable rows={drag.rows} onSelect={setSelectedDrag} />
              </Panel>
            ) : (
              <Panel
                title={track === 'dry' ? 'Dry-run twins' : 'Positions'}
                action={<Segmented value={positionFilter} values={['open', 'closed', 'all']} onChange={setPositionFilter} />}
              >
                <PositionsTable rows={positions} onSelect={setSelectedPosition} />
              </Panel>
            )}
          </section>

          <section class="lower-grid">
            <Panel title="Notifications">
              <EventList rows={notifications} empty="No notifications yet" compact onSelect={setSelectedEvent} />
            </Panel>
            <Panel title="Recent Guardrails">
              <Candidates rows={candidates} onSelect={setSelectedCandidate} />
            </Panel>
            <Panel title="Breakers">
              <BreakerList rows={breakers} />
            </Panel>
          </section>

          <section class="table-zone">
            <Panel
              title="Why candidates were vetoed (24h)"
              action={<a class="export-link" href="/api/reports/funnel.csv?range=24h">Checks CSV</a>}
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
                    <a class="export-link" href={`/api/reports/veto-dry-run-summary.csv?range=${vetoDryRunRange}`}>
                      Summary CSV
                    </a>
                    <a class="export-link" href={`/api/reports/veto-dry-run.csv?range=${vetoDryRunRange}`}>
                      Detail CSV
                    </a>
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
            <Panel title="Relaxed-risk rollout (24h)">
              <RelaxedRiskPanel analytics={relaxedRisk} />
            </Panel>
          </section>
        </main>
      </div>

      {selectedPosition && <PositionModal position={selectedPosition} onClose={closeDetails} />}
      {selectedEvent && <EventModal event={selectedEvent} onClose={closeDetails} />}
      {selectedCandidate && <CandidateModal candidate={selectedCandidate} onClose={closeDetails} />}
      {selectedShadow && <ShadowOutcomeModal outcome={selectedShadow} onClose={closeDetails} />}
      {selectedDrag && <DragModal row={selectedDrag} onClose={closeDetails} />}
    </div>
  );
}

function Sidebar() {
  return (
    <aside class="sidebar">
      <div class="brand-row">
        <div class="brand-mark">pf</div>
        <div>
          <strong>PumpDesk</strong>
          <span>Operator UI</span>
        </div>
      </div>
      <nav class="sidebar-links" aria-label="Creator links">
        <span class="sidebar-links-label">Creator</span>
        <a href="https://www.nishimweprince.dev/" target="_blank" rel="noopener noreferrer">Website</a>
        <a href="https://github.com/nishimweprince" target="_blank" rel="noopener noreferrer">GitHub</a>
        <a href="https://github.com/nishimweprince/trading-algos/tree/main/pump-fun" target="_blank" rel="noopener noreferrer">Source repo</a>
      </nav>
    </aside>
  );
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
function PnlChart({ datasets }: { datasets: ChartSeries[] }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const multi = datasets.length > 1;

  useEffect(() => {
    if (!canvasRef.current) return;
    const labelSource = datasets.reduce(
      (longest, d) => (d.points.length > longest.length ? d.points : longest),
      [] as PnlPoint[],
    );
    chartRef.current?.destroy();
    chartRef.current = new Chart(canvasRef.current, {
      type: 'line',
      data: {
        labels: labelSource.map((p) => formatMonthish(p.time)),
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
        plugins: { tooltip: { displayColors: multi }, legend: { display: false } },
        scales: {
          x: { ticks: { maxTicksLimit: 7, color: '#9b9f9f' }, grid: { display: false } },
          y: { ticks: { color: '#9b9f9f' }, grid: { color: 'rgba(31, 39, 42, 0.08)' } },
        },
      },
    });
    return () => chartRef.current?.destroy();
  }, [datasets]);

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

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} ${res.status}`);
  return (await res.json()) as T;
}

function zeroNumber(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
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
