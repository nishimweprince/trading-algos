import { formatDollars, formatPerformance, formatPct } from "@/lib/format";
import { closedCount, winRate } from "@/lib/stats";
import type { BacktestReport, PerformanceUnit } from "@/lib/types";
import { cn } from "@/lib/utils";

interface KpiStripProps {
  report: BacktestReport | null;
  unit: PerformanceUnit;
}

export function KpiStrip({ report, unit }: KpiStripProps) {
  const longN = report ? closedCount(report.long_wins, report.long_be, report.long_loss) : 0;
  const shortN = report ? closedCount(report.short_wins, report.short_be, report.short_loss) : 0;
  const combined = report
    ? winRate(
        report.long_wins + report.short_wins,
        report.long_be + report.short_be,
        report.long_loss + report.short_loss,
      )
    : null;

  const performanceTiles = [
    {
      n: "01",
      label: "Realized",
      value: report
        ? formatPerformance(report.realized_pips, report.realized_dollars, unit)
        : "—",
      tone: report
        ? unit === "dollars"
          ? (report.realized_dollars ?? undefined)
          : report.realized_pips
        : undefined,
    },
    {
      n: "02",
      label: "Open",
      value: report
        ? formatPerformance(report.unrealized_pips, report.unrealized_dollars, unit)
        : "—",
      tone: report
        ? unit === "dollars"
          ? (report.unrealized_dollars ?? undefined)
          : report.unrealized_pips
        : undefined,
    },
    {
      n: "03",
      label: "Max drawdown",
      value: report
        ? formatPerformance(
            -report.max_drawdown_pips,
            report.max_drawdown_dollars === null ? null : -report.max_drawdown_dollars,
            unit,
          )
        : "—",
      tone: report && report.max_drawdown_pips > 0 ? -1 : undefined,
    },
    {
      n: "04",
      label: "Locks",
      value: report ? String(report.locks) : "—",
    },
    {
      n: "05",
      label: "Win rate",
      value: report ? formatPct(combined) : "—",
      hint: report ? `${longN + shortN} closed` : undefined,
    },
  ];
  const tiles =
    unit === "dollars"
      ? [
          {
            n: "00",
            label: "Equity",
            value:
              report && report.equity_dollars !== null
                ? formatDollars(report.equity_dollars)
                : "—",
            tone: report?.equity_dollars ?? undefined,
          },
          ...performanceTiles,
        ]
      : performanceTiles;

  return (
    <div
      className={cn(
        "grid grid-cols-2 border-b border-border",
        unit === "dollars" ? "md:grid-cols-3 xl:grid-cols-6" : "md:grid-cols-5",
      )}
    >
      {tiles.map((tile) => (
        <div
          key={tile.n}
          className="flex min-h-[108px] flex-col justify-between bg-inverted p-4 text-inverted-foreground"
        >
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-inverted-foreground/50">{tile.n}</span>
            <span className="text-[11px] font-medium uppercase">{tile.label}</span>
          </div>
          <div>
            <div
              className={cn(
                "text-sm",
                tile.tone !== undefined && tile.tone > 0 && "text-win",
                tile.tone !== undefined && tile.tone < 0 && "text-loss",
              )}
            >
              {tile.value}
            </div>
            {tile.hint ? (
              <div className="mt-0.5 text-[11px] text-inverted-foreground/50">{tile.hint}</div>
            ) : null}
          </div>
        </div>
      ))}
    </div>
  );
}
