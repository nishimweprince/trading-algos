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
  };
  positions: { openCount: number; openExposureSol: number; maxConcurrent: number };
  flow: { graduations: number; accepted: number; vetoed: number; highVolatility: number };
  system: { latestBreaker: { type: string; detail: string | null; tripped: boolean; at: string } | null; latestEventAt: string | null };
}

interface PositionRow {
  id: number;
  mint: string;
  state: string;
  entryPrice: number | null;
  sizeSol: number;
  exitReason: string | null;
  pnlSol: number | null;
  pnlPct: number | null;
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
}

interface OperatorEvent {
  id: number;
  category: string;
  level: Level;
  message: string;
  entityMint: string | null;
  createdAt: string;
}

type PositionFilter = 'open' | 'closed' | 'all';
type PnlRange = '24h' | '7d' | '30d';
type DashboardStatus = 'connecting' | 'live' | 'offline' | 'reconnecting';

const emptySummary: Summary = {
  mode: 'paper',
  pnl: { realizedSol: 0, realized24hSol: 0, realized7dSol: 0, winRatePct: 0, closedCount: 0, wins: 0, losses: 0 },
  positions: { openCount: 0, openExposureSol: 0, maxConcurrent: 0 },
  flow: { graduations: 0, accepted: 0, vetoed: 0, highVolatility: 0 },
  system: { latestBreaker: null, latestEventAt: null },
};

function App() {
  const [summary, setSummary] = useState<Summary>(emptySummary);
  const [positions, setPositions] = useState<PositionRow[]>([]);
  const [pnl, setPnl] = useState<PnlPoint[]>([]);
  const [candidates, setCandidates] = useState<CandidateRow[]>([]);
  const [events, setEvents] = useState<OperatorEvent[]>([]);
  const [positionFilter, setPositionFilter] = useState<PositionFilter>('open');
  const [pnlRange, setPnlRange] = useState<PnlRange>('7d');
  const [status, setStatus] = useState<DashboardStatus>('connecting');
  const [streamReady, setStreamReady] = useState(false);

  const refresh = async () => {
    await fetchJson('/api/health');
    const [summaryRes, positionsRes, pnlRes, candidatesRes, eventsRes] = await Promise.all([
      fetchJson<Summary>('/api/dashboard/summary'),
      fetchJson<PositionRow[]>(`/api/positions?state=${positionFilter}&limit=40`),
      fetchJson<PnlPoint[]>(`/api/pnl?range=${pnlRange}`),
      fetchJson<CandidateRow[]>('/api/candidates?limit=12'),
      fetchJson<OperatorEvent[]>('/api/events?limit=40'),
    ]);
    setSummary(summaryRes);
    setPositions(positionsRes);
    setPnl(pnlRes);
    setCandidates(candidatesRes);
    setEvents(eventsRes);
    setStatus('live');
    setStreamReady(true);
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

          <section class="metric-grid" aria-label="Wallet metrics">
            <KpiCard
              label="Realized PnL"
              value={formatSol(summary.pnl.realizedSol)}
              detail={`${summary.pnl.closedCount} closed positions`}
              tone={summary.pnl.realizedSol >= 0 ? 'profit' : 'loss'}
            />
            <KpiCard
              label="Open Exposure"
              value={`${summary.positions.openExposureSol.toFixed(3)} SOL`}
              detail={`${summary.positions.openCount}/${summary.positions.maxConcurrent} positions`}
            />
            <KpiCard
              label="Win Rate"
              value={`${summary.pnl.winRatePct.toFixed(0)}%`}
              detail={`${summary.pnl.wins} wins, ${summary.pnl.losses} losses`}
              tone={summary.pnl.winRatePct >= 50 ? 'profit' : 'loss'}
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

            <Panel title="Operator Recommendations">
              <RecommendationList summary={summary} />
            </Panel>
          </section>

          <section class="table-zone">
            <Panel
              title="Positions"
              action={<Segmented value={positionFilter} values={['open', 'closed', 'all']} onChange={setPositionFilter} />}
            >
              <PositionsTable rows={positions} />
            </Panel>
          </section>

          <section class="lower-grid">
            <Panel title="Notifications">
              <EventList rows={notifications} empty="No notifications yet" compact />
            </Panel>
            <Panel title="Recent Guardrails">
              <Candidates rows={candidates} />
            </Panel>
            <Panel title="System Events">
              <EventList rows={events} empty="No system events yet" compact />
            </Panel>
          </section>
        </main>
      </div>
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

function RecommendationList({ summary }: { summary: Summary }) {
  const breaker = summary.system.latestBreaker;
  const rows = [
    {
      title: 'Exposure',
      body: summary.positions.openCount >= summary.positions.maxConcurrent ? 'Maximum concurrent positions reached' : 'Capacity remains available',
      value: `${summary.positions.openCount}/${summary.positions.maxConcurrent}`,
    },
    {
      title: 'Guardrails',
      body: `${summary.flow.accepted} accepted, ${summary.flow.vetoed} vetoed`,
      value: `${summary.flow.highVolatility} high-vol`,
    },
    {
      title: 'Breaker',
      body: breaker ? breaker.detail ?? breaker.type : 'No breaker events recorded',
      value: breaker?.tripped ? 'Tripped' : 'Clear',
    },
  ];

  return (
    <div class="recommendations">
      {rows.map((row) => (
        <div class="recommendation" key={row.title}>
          <div>
            <strong>{row.title}</strong>
            <span>{row.body}</span>
          </div>
          <b>{row.value}</b>
        </div>
      ))}
      <div class="total-box">Net realized: {formatSol(summary.pnl.realizedSol)}</div>
    </div>
  );
}

function PositionsTable({ rows }: { rows: PositionRow[] }) {
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
            <tr key={row.id}>
              <td class="mono">{short(row.mint)}</td>
              <td><span class={`pill ${row.state.toLowerCase()}`}>{row.state}</span></td>
              <td>{row.sizeSol.toFixed(3)} SOL</td>
              <td>{row.entryPrice ? row.entryPrice.toPrecision(4) : '-'}</td>
              <td class={row.pnlSol === null ? '' : row.pnlSol >= 0 ? 'profit-text' : 'loss-text'}>
                {row.pnlSol === null ? '-' : `${formatSol(row.pnlSol)} (${(row.pnlPct ?? 0).toFixed(1)}%)`}
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

function EventList({ rows, empty, compact }: { rows: OperatorEvent[]; empty: string; compact?: boolean }) {
  if (rows.length === 0) return <div class="empty">{empty}</div>;
  return (
    <ol class={`events ${compact ? 'compact' : ''}`}>
      {rows.map((event) => (
        <li class={event.level} key={event.id}>
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

function Candidates({ rows }: { rows: CandidateRow[] }) {
  if (rows.length === 0) return <div class="empty">No guardrail decisions yet</div>;
  return (
    <div class="candidates">
      {rows.map((row) => (
        <div class="candidate" key={row.id}>
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

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} ${res.status}`);
  return (await res.json()) as T;
}

function formatSol(value: number): string {
  return `${value >= 0 ? '+' : ''}${value.toFixed(4)} SOL`;
}

function short(value: string): string {
  return value.length > 12 ? `${value.slice(0, 5)}...${value.slice(-5)}` : value;
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatMonthish(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

render(<App />, document.getElementById('app')!);
