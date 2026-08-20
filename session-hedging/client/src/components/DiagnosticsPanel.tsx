import {
  concurrencyTimeline,
  excursionPoints,
  holdingDistribution,
  performanceBreakdown,
  rHistogram,
  type HistogramBucket,
} from "@/lib/stats";
import { SESSION_LABEL, type BacktestReport, type S7ResearchArtifact } from "@/lib/types";

function Bars({ rows }: { rows: HistogramBucket[] }) {
  const max = Math.max(1, ...rows.map((row) => row.count));
  return (
    <div className="space-y-2">
      {rows.map((row) => (
        <div key={row.label} className="grid grid-cols-[64px_1fr_28px] items-center gap-2 text-[11px]">
          <span className="text-muted-foreground">{row.label}</span>
          <div className="h-2 bg-accent">
            <div className="h-full bg-foreground" style={{ width: `${(row.count / max) * 100}%` }} />
          </div>
          <span className="text-right tabular-nums">{row.count}</span>
        </div>
      ))}
    </div>
  );
}

function BreakdownTable({ report, by }: { report: BacktestReport; by: "session" | "weekday" }) {
  const rows = performanceBreakdown(report.trade_pairs, by);
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[420px] text-left text-[11px]">
        <thead className="text-muted-foreground">
          <tr className="border-b border-border">
            <th className="py-2 font-normal">{by}</th>
            <th className="py-2 text-right font-normal">N</th>
            <th className="py-2 text-right font-normal">gross / net pips</th>
            <th className="py-2 text-right font-normal">gross / net R</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.label} className="border-b border-border/60 last:border-0">
              <td className="py-2 capitalize">{SESSION_LABEL[row.label] ?? row.label.replace("_", " ")}</td>
              <td className="py-2 text-right tabular-nums">{row.structures}</td>
              <td className="py-2 text-right tabular-nums">{row.grossPips.toFixed(1)} / {row.netPips.toFixed(1)}</td>
              <td className="py-2 text-right tabular-nums">{row.grossR.toFixed(2)} / {row.netR.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 ? <p className="py-4 text-[11px] text-muted-foreground">No completed structures.</p> : null}
    </div>
  );
}

function ExcursionPlot({ report }: { report: BacktestReport }) {
  const points = excursionPoints(report.trade_pairs);
  const maxX = Math.max(1, ...points.map((point) => Math.abs(point.mae)));
  const maxY = Math.max(1, ...points.map((point) => Math.abs(point.mfe)));
  return (
    <div>
      <svg viewBox="0 0 300 120" className="h-32 w-full" role="img" aria-label="MAE versus MFE scatter plot">
        <line x1="18" y1="104" x2="294" y2="104" stroke="currentColor" opacity="0.2" />
        <line x1="18" y1="8" x2="18" y2="104" stroke="currentColor" opacity="0.2" />
        {points.map((point, index) => (
          <circle
            key={`${point.side}-${index}`}
            cx={18 + (Math.abs(point.mae) / maxX) * 276}
            cy={104 - (Math.abs(point.mfe) / maxY) * 96}
            r="2.5"
            fill="currentColor"
            opacity={point.side === "long" ? 0.9 : 0.45}
          />
        ))}
      </svg>
      <p className="text-[10px] text-muted-foreground">x: |MAE| pips · y: |MFE| pips · dark long / light short</p>
    </div>
  );
}

function ConcurrencyPlot({ report }: { report: BacktestReport }) {
  const points = concurrencyTimeline(report.trade_pairs);
  const max = Math.max(1, ...points.map((point) => point.count));
  const path = points
    .map((point, index) => {
      const x = points.length === 1 ? 0 : (index / (points.length - 1)) * 300;
      const y = 104 - (point.count / max) * 96;
      return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <div>
      <svg viewBox="0 0 300 112" className="h-32 w-full" role="img" aria-label="Concurrent structures timeline">
        <path d={path} fill="none" stroke="currentColor" strokeWidth="2" />
      </svg>
      <p className="text-[10px] text-muted-foreground">Observed maximum {report.max_concurrent_structures} · median {report.median_concurrent?.toFixed(1) ?? "—"}</p>
    </div>
  );
}

export function DiagnosticsPanel({
  report,
  s7,
}: {
  report: BacktestReport;
  s7: S7ResearchArtifact | null;
}) {
  const breachDays = new Set(
    report.events.filter((event) => event.kind === "prop_guard_breached").map((event) => event.ts.slice(0, 10)),
  ).size;
  return (
    <section id="diagnostics" className="scroll-mt-4 border-b border-border px-5 py-7 md:px-10">
      <div className="mb-5">
        <h2 className="text-sm font-medium">Diagnostics</h2>
        <p className="mt-1 text-[11px] text-muted-foreground">Completed structures; gross and net are kept side by side.</p>
      </div>
      <div className="grid gap-px bg-border lg:grid-cols-2 xl:grid-cols-4">
        <div className="bg-background p-4"><p className="mb-4 text-[11px] uppercase text-muted-foreground">Net R histogram</p><Bars rows={rHistogram(report.trade_pairs)} /></div>
        <div className="bg-background p-4">
          <p className="mb-4 text-[11px] uppercase text-muted-foreground">Holding time</p>
          <p className="mb-3 text-[11px] tabular-nums text-muted-foreground">
            median {report.median_hold_hours == null ? "—" : `${report.median_hold_hours.toFixed(1)}h`}
            {" · "}
            p95 {report.p95_hold_hours == null ? "—" : `${report.p95_hold_hours.toFixed(1)}h`}
          </p>
          <Bars rows={holdingDistribution(report.trade_pairs)} />
        </div>
        <div className="bg-background p-4"><p className="mb-2 text-[11px] uppercase text-muted-foreground">MAE / MFE</p><ExcursionPlot report={report} /></div>
        <div className="bg-background p-4"><p className="mb-2 text-[11px] uppercase text-muted-foreground">Concurrency</p><ConcurrencyPlot report={report} /></div>
      </div>
      <div className="mt-px grid gap-px bg-border lg:grid-cols-2">
        <div className="bg-background p-4"><BreakdownTable report={report} by="session" /></div>
        <div className="bg-background p-4"><BreakdownTable report={report} by="weekday" /></div>
      </div>
      <div className="mt-px grid gap-px bg-border sm:grid-cols-4">
        <div className="bg-background p-4"><p className="text-[10px] uppercase text-muted-foreground">PropGuard (this backtest)</p><p className="mt-2 text-sm">{report.prop_guard_breached ? "Breached" : "Clear"}</p></div>
        <div className="bg-background p-4"><p className="text-[10px] uppercase text-muted-foreground">Backtest breach-event days</p><p className="mt-2 text-sm tabular-nums">{breachDays}</p></div>
        <div className="bg-background p-4">
          <p className="text-[10px] uppercase text-muted-foreground">S7 worst simulated path (research)</p>
          <p className="mt-2 text-sm tabular-nums">
            {s7
              ? `${s7.modes
                  .map((mode) => mode.worst_simulated_path_net_r.toFixed(2))
                  .join(" / ")}R net`
              : "Unavailable"}
          </p>
        </div>
        <div className="bg-background p-4">
          <p className="text-[10px] uppercase text-muted-foreground">S7 min free margin p50 / headroom path</p>
          <p className="mt-2 text-sm tabular-nums">
            {s7
              ? s7.modes
                  .map((mode) => `${mode.headroom_path.p50.toFixed(1)}%`)
                  .join(" · ")
              : "Unavailable"}
          </p>
        </div>
      </div>
      {s7 ? (
        <div className="mt-px overflow-x-auto bg-background p-4">
          <p className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
            S7 research simulation · not interactive backtest · not broker facts
          </p>
          <table className="mt-3 w-full min-w-[720px] text-left text-[11px]">
            <thead className="text-muted-foreground">
              <tr className="border-b border-border">
                <th className="py-2 font-normal">Mode</th>
                <th className="py-2 font-normal">Worst path net R / pips</th>
                <th className="py-2 font-normal">Daily 3%/5% breach days</th>
                <th className="py-2 font-normal">Total 6%/10% breach days</th>
                <th className="py-2 font-normal">Min free-margin p01/p50</th>
              </tr>
            </thead>
            <tbody>
              {s7.modes.map((mode) => (
                <tr key={mode.entry_mode} className="border-b border-border/60 last:border-0">
                  <td className="py-2">{mode.entry_mode}</td>
                  <td className="py-2 tabular-nums">
                    {mode.worst_simulated_path_net_r.toFixed(2)}R / {mode.worst_simulated_path_net_pips.toFixed(1)}
                  </td>
                  <td className="py-2 tabular-nums">
                    {mode.daily_breach_days["3"]?.breach_count ?? 0} / {mode.daily_breach_days["5"]?.breach_count ?? 0}
                  </td>
                  <td className="py-2 tabular-nums">
                    {mode.total_breach_days["6"]?.breach_count ?? 0} / {mode.total_breach_days["10"]?.breach_count ?? 0}
                  </td>
                  <td className="py-2 tabular-nums">
                    {mode.minimum_free_margin_pct_distribution.p01.toFixed(2)}% /{" "}
                    {mode.minimum_free_margin_pct_distribution.p50.toFixed(2)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <ul className="mt-3 list-disc space-y-1 pl-4 text-[10px] text-muted-foreground">
            {s7.source.caveats.map((caveat) => (
              <li key={caveat}>{caveat}</li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="mt-3 text-[10px] text-muted-foreground">
          S7 research artifact is not loaded. The interactive backtest has no broker margin series
          or Monte Carlo path; those fields remain unverified here.
        </p>
      )}
    </section>
  );
}
