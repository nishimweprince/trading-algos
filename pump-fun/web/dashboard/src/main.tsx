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
  hardChecks?: Array<{ id: string; label?: string; status: string; detail?: string }>;
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
type DashboardStatus = 'connecting' | 'live' | 'offline' | 'reconnecting';

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
  const [breakers, setBreakers] = useState<BreakerRow[]>([]);
  const [positionFilter, setPositionFilter] = useState<PositionFilter>('open');
  const [pnlRange, setPnlRange] = useState<PnlRange>('7d');
  const [status, setStatus] = useState<DashboardStatus>('connecting');
  const [streamReady, setStreamReady] = useState(false);
  const [manualRefreshing, setManualRefreshing] = useState(false);
  const [selectedPosition, setSelectedPosition] = useState<PositionRow | null>(null);
  const [selectedEvent, setSelectedEvent] = useState<OperatorEvent | null>(null);
  const [selectedCandidate, setSelectedCandidate] = useState<CandidateRow | null>(null);

  const refresh = async () => {
    await fetchJson('/api/health');
    const [summaryRes, positionsRes, pnlRes, candidatesRes, eventsRes, riskRes, funnelRes, vetoRes, breakersRes] = await Promise.all([
      fetchJson<Summary>('/api/dashboard/summary'),
      fetchJson<PositionRow[]>(`/api/positions?state=${positionFilter}&limit=40`),
      fetchJson<PnlPoint[]>(`/api/pnl?range=${pnlRange}`),
      fetchJson<CandidateRow[]>('/api/candidates?limit=12'),
      fetchJson<OperatorEvent[]>('/api/events?limit=40'),
      fetchJson<RiskStatus>('/api/risk/status'),
      fetchJson<FunnelStats>('/api/analytics/funnel?range=24h'),
      fetchJson<VetoBreakdown>('/api/analytics/veto-reasons?range=24h'),
      fetchJson<BreakerRow[]>('/api/breakers?limit=8'),
    ]);
    setSummary(normalizeSummary(summaryRes));
    setPositions(Array.isArray(positionsRes) ? positionsRes.map(normalizePosition) : []);
    setPnl(Array.isArray(pnlRes) ? pnlRes.map(normalizePnlPoint) : []);
    setCandidates(Array.isArray(candidatesRes) ? candidatesRes.map(normalizeCandidate) : []);
    setEvents(Array.isArray(eventsRes) ? eventsRes : []);
    setRisk(riskRes ?? null);
    setFunnel(funnelRes ?? null);
    setVetoBreakdown(vetoRes ?? null);
    setBreakers(Array.isArray(breakersRes) ? breakersRes : []);
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
  }, [positionFilter, pnlRange]);

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
  }, [streamReady, status, positionFilter, pnlRange]);

  const modalOpen = selectedPosition !== null || selectedEvent !== null || selectedCandidate !== null;
  const closeDetails = () => {
    setSelectedPosition(null);
    setSelectedEvent(null);
    setSelectedCandidate(null);
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
              <div class="export-row">
                <a class="export-link" href="/api/reports/trades.csv?range=7d">Trades CSV</a>
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

          <section class="content-grid">
            <Panel
              title="PnL vs Time"
              wide
              action={<Segmented value={pnlRange} values={['24h', '7d', '30d']} onChange={setPnlRange} />}
            >
              <div class="chart-header">
                <div>
                  <span>Current curve</span>
                  <strong class={finalPnl >= 0 ? 'profit-text' : 'loss-text'}>{formatSol(finalPnl)}</strong>
                </div>
                <div class="legend">
                  <span class="legend-dot pnl" />
                  <span>Realized PnL</span>
                </div>
              </div>
              <PnlChart points={pnl} positive={finalPnl >= 0} />
            </Panel>

            <Panel title="Risk & Ops">
              <RiskPanel risk={risk} summary={summary} funnel={funnel} />
            </Panel>
          </section>

          <section class="table-zone">
            <Panel
              title="Positions"
              action={<Segmented value={positionFilter} values={['open', 'closed', 'all']} onChange={setPositionFilter} />}
            >
              <PositionsTable rows={positions} onSelect={setSelectedPosition} />
            </Panel>
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
        </main>
      </div>

      {selectedPosition && <PositionModal position={selectedPosition} onClose={closeDetails} />}
      {selectedEvent && <EventModal event={selectedEvent} onClose={closeDetails} />}
      {selectedCandidate && <CandidateModal candidate={selectedCandidate} onClose={closeDetails} />}
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
    </aside>
  );
}

function KpiCard(props: { label: string; value: string; detail: string; tone?: 'profit' | 'loss' }) {
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

function Segmented<T extends string>(props: { value: T; values: T[]; onChange: (value: T) => void }) {
  return (
    <div class="segmented">
      {props.values.map((value) => (
        <button type="button" class={props.value === value ? 'active' : ''} onClick={() => props.onChange(value)}>
          {value}
        </button>
      ))}
    </div>
  );
}

function PnlChart({ points, positive }: { points: PnlPoint[]; positive: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart | null>(null);

  useEffect(() => {
    if (!canvasRef.current) return;
    const color = positive ? '#0d7a59' : '#b94a48';
    const fill = positive ? 'rgba(13, 122, 89, 0.1)' : 'rgba(185, 74, 72, 0.1)';
    chartRef.current?.destroy();
    chartRef.current = new Chart(canvasRef.current, {
      type: 'line',
      data: {
        labels: points.map((p) => formatMonthish(p.time)),
        datasets: [
          {
            data: points.map((p) => p.cumulativePnlSol),
            borderColor: color,
            backgroundColor: fill,
            fill: true,
            tension: 0.34,
            pointRadius: 2,
            pointHoverRadius: 4,
            borderWidth: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { tooltip: { displayColors: false }, legend: { display: false } },
        scales: {
          x: { ticks: { maxTicksLimit: 7, color: '#9b9f9f' }, grid: { display: false } },
          y: { ticks: { color: '#9b9f9f' }, grid: { color: 'rgba(31, 39, 42, 0.08)' } },
        },
      },
    });
    return () => chartRef.current?.destroy();
  }, [points, positive]);

  if (points.length === 0) return <div class="empty chart-empty">No closed positions in this window</div>;
  return (
    <div class="chart-box">
      <canvas ref={canvasRef} class="chart" />
    </div>
  );
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
              <td><span class={`pill ${row.state.toLowerCase()}`}>{row.state}</span></td>
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
        </div>
        {candidate.hardChecks && candidate.hardChecks.length > 0 && (
          <div class="payload-panel">
            <span>Hard checks</span>
            <div class="check-chips">
              {candidate.hardChecks.map((check) => (
                <span class={`check-chip ${check.status}`} key={check.id} title={check.detail ?? check.label ?? check.id}>
                  {check.id}:{check.status}
                </span>
              ))}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

function DetailRow(props: { label: string; value: string; tone?: 'profit' | 'loss'; mono?: boolean }) {
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
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
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
