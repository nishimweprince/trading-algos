import {
  concurrencyTimeline,
  excursionPoints,
  holdingDistribution,
  performanceBreakdown,
  rHistogram,
  type HistogramBucket,
} from "@/lib/stats";
import { SESSION_LABEL, type BacktestReport } from "@/lib/types";

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

export function DiagnosticsPanel({ report }: { report: BacktestReport }) {
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
        <div className="bg-background p-4"><p className="mb-4 text-[11px] uppercase text-muted-foreground">Holding time</p><Bars rows={holdingDistribution(report.trade_pairs)} /></div>
        <div className="bg-background p-4"><p className="mb-2 text-[11px] uppercase text-muted-foreground">MAE / MFE</p><ExcursionPlot report={report} /></div>
        <div className="bg-background p-4"><p className="mb-2 text-[11px] uppercase text-muted-foreground">Concurrency</p><ConcurrencyPlot report={report} /></div>
      </div>
      <div className="mt-px grid gap-px bg-border lg:grid-cols-2">
        <div className="bg-background p-4"><BreakdownTable report={report} by="session" /></div>
        <div className="bg-background p-4"><BreakdownTable report={report} by="weekday" /></div>
      </div>
      <div className="mt-px grid gap-px bg-border sm:grid-cols-4">
        <div className="bg-background p-4"><p className="text-[10px] uppercase text-muted-foreground">PropGuard</p><p className="mt-2 text-sm">{report.prop_guard_breached ? "Breached" : "Clear"}</p></div>
        <div className="bg-background p-4"><p className="text-[10px] uppercase text-muted-foreground">Breach days</p><p className="mt-2 text-sm tabular-nums">{breachDays}</p></div>
        <div className="bg-background p-4"><p className="text-[10px] uppercase text-muted-foreground">Worst simulated day</p><p className="mt-2 text-sm">Unavailable*</p></div>
        <div className="bg-background p-4"><p className="text-[10px] uppercase text-muted-foreground">Min free margin / headroom path</p><p className="mt-2 text-sm">Unavailable*</p></div>
      </div>
      <p className="mt-3 text-[10px] text-muted-foreground">* The interactive backtest has no broker margin series or Monte Carlo path. Those fields remain unverified here; S7 is a separate research artifact and not a prop-firm claim.</p>
    </section>
  );
}
