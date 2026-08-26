import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { faRotate } from "@fortawesome/free-solid-svg-icons";
import { DivergencePanel } from "@/components/DivergencePanel";
import { EquityDrawdownChart } from "@/components/EquityDrawdownChart";
import { TradeBlotter } from "@/components/TradeBlotter";
import { fetchExecutionStatus, fetchPaperStatus } from "@/lib/api";
import { formatPct, formatUnit, formatWhen } from "@/lib/format";
import { Icon } from "@/lib/icon";
import { closedCount, pairSessionBreakdown, winRateExclBe } from "@/lib/stats";
import { SESSION_LABEL, type ExecutionStatus, type PaperStatus, type TradePairResult } from "@/lib/types";

const POLL_MS = 15_000;
const UNIT = "pips" as const;

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="border border-border px-3 py-2">
      <p className="text-[10px] uppercase text-muted-foreground">{label}</p>
      <p className="mt-1 text-sm tabular-nums">{value}</p>
      {hint ? <p className="mt-0.5 text-[10px] text-muted-foreground">{hint}</p> : null}
    </div>
  );
}

/** Realised P&L by calendar day, mirroring how the backtest reports by session. */
function dayBreakdown(pairs: TradePairResult[]): { day: string; pnl: number; n: number }[] {
  const map = new Map<string, { day: string; pnl: number; n: number }>();
  for (const pair of pairs) {
    const day = pair.entry_ts.slice(0, 10);
    const row = map.get(day) ?? { day, pnl: 0, n: 0 };
    row.pnl += pair.net_pnl_pips ?? pair.pnl_pips ?? 0;
    row.n += 1;
    map.set(day, row);
  }
  return [...map.values()].sort((a, b) => b.day.localeCompare(a.day)).slice(0, 14);
}

export function LivePerformancePage() {
  const [paper, setPaper] = useState<PaperStatus | null>(null);
  const [execution, setExecution] = useState<ExecutionStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshedAt, setRefreshedAt] = useState<Date | null>(null);
  const inFlight = useRef(false);

  const load = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const status = await fetchPaperStatus();
      setPaper(status);
      // Execution is a separate, strictly-authenticated route. A failure there must not
      // blank out the engine view, which is the part that always exists.
      try {
        setExecution(await fetchExecutionStatus());
      } catch {
        setExecution(null);
      }
      setError(null);
      setRefreshedAt(new Date());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not reach the service");
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    // Unmounting stops the polling, which is why this is a view rather than a section
    // of the backtest page.
    return () => window.clearInterval(timer);
  }, [load]);

  const closed = paper?.trade_pairs ?? [];
  const sessions = useMemo(() => pairSessionBreakdown(closed, UNIT), [closed]);
  const days = useMemo(() => dayBreakdown(closed), [closed]);

  const stats = paper?.stats;
  const wins = (stats?.long_wins ?? 0) + (stats?.short_wins ?? 0);
  const be = (stats?.long_be ?? 0) + (stats?.short_be ?? 0);
  const loss = (stats?.long_loss ?? 0) + (stats?.short_loss ?? 0);

  return (
    <div className="space-y-10">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-sm font-medium">Live performance</h2>
          <p className="mt-1 text-[11px] text-muted-foreground">
            {paper?.enabled === false
              ? "The paper loop is disabled; nothing is being tracked."
              : `Last closed bar ${paper?.last_ts ? formatWhen(paper.last_ts) : "—"}.`}
          </p>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-muted-foreground">
          <span>{refreshedAt ? `updated ${refreshedAt.toLocaleTimeString()}` : "loading…"}</span>
          <button
            type="button"
            onClick={() => void load()}
            className="flex items-center gap-1.5 border border-border px-2 py-1 hover:bg-muted"
          >
            <Icon icon={faRotate} className="h-3 w-3" />
            Refresh
          </button>
        </div>
      </header>

      {error ? (
        <p className="border border-red-500/40 bg-red-500/5 px-3 py-2 text-xs text-red-500">
          {error}
        </p>
      ) : null}

      {paper?.prop_guard_breached ? (
        <p className="border border-red-500/40 bg-red-500/5 px-3 py-2 text-xs text-red-500">
          Prop guard breached — {paper.prop_guard_breach_reason ?? "limit reached"}. No new
          structures will be opened.
        </p>
      ) : null}

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
        <Tile
          label="Realised"
          value={formatUnit(stats?.realized_pips ?? 0, UNIT)}
          hint={`${closedCount(wins, be, loss)} closed`}
        />
        <Tile label="Win rate" value={formatPct(winRateExclBe(wins, be, loss))} hint="excl. BE" />
        <Tile label="Open structures" value={String(paper?.open_pairs.length ?? 0)} />
        <Tile
          label="Pending brackets"
          value={String(paper?.pending_entry_orders.length ?? 0)}
          hint="resting at the broker when live"
        />
        <Tile
          label="Stops moved"
          value={String(stats?.locks ?? 0)}
          hint="breakeven ratchet arms"
        />
      </div>

      {execution ? <DivergencePanel status={execution} /> : null}

      <EquityDrawdownChart points={paper?.equity_curve ?? []} unit={UNIT} />

      <section className="grid gap-8 lg:grid-cols-2">
        <div>
          <h3 className="mb-2 text-sm font-medium">By session</h3>
          {sessions.length === 0 ? (
            <p className="text-xs text-muted-foreground">No closed structures yet.</p>
          ) : (
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border text-left text-[10px] uppercase text-muted-foreground">
                  <th className="py-2 font-normal">Session</th>
                  <th className="py-2 text-right font-normal">W/BE/L</th>
                  <th className="py-2 text-right font-normal">Net</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((row) => (
                  <tr key={row.session} className="border-b border-border last:border-b-0">
                    <td className="py-2">{SESSION_LABEL[row.session] ?? row.session}</td>
                    <td className="py-2 text-right tabular-nums">
                      {row.wins}/{row.be}/{row.loss}
                    </td>
                    <td className="py-2 text-right tabular-nums">{formatUnit(row.pnl, UNIT)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div>
          <h3 className="mb-2 text-sm font-medium">By day</h3>
          {days.length === 0 ? (
            <p className="text-xs text-muted-foreground">No closed structures yet.</p>
          ) : (
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border text-left text-[10px] uppercase text-muted-foreground">
                  <th className="py-2 font-normal">Day</th>
                  <th className="py-2 text-right font-normal">Trades</th>
                  <th className="py-2 text-right font-normal">Net</th>
                </tr>
              </thead>
              <tbody>
                {days.map((row) => (
                  <tr key={row.day} className="border-b border-border last:border-b-0">
                    <td className="py-2 tabular-nums">{row.day}</td>
                    <td className="py-2 text-right tabular-nums">{row.n}</td>
                    <td className="py-2 text-right tabular-nums">{formatUnit(row.pnl, UNIT)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section>
        <h3 className="mb-2 text-sm font-medium">Closed structures</h3>
        <TradeBlotter pairs={closed} unit={UNIT} context={null} />
      </section>
    </div>
  );
}
