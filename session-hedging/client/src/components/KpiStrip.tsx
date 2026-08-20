import { formatDollars, formatPct, formatPipsAndR, formatPp } from "@/lib/format";
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
      value: report ? formatPipsAndR(report.realized_pips, report.realized_r) : "—",
      hint:
        unit === "dollars" && report?.realized_dollars !== null && report
          ? formatDollars(report.realized_dollars)
          : undefined,
      tone: report?.realized_pips,
    },
    {
      n: "02",
      label: "Open",
      value: report ? formatPipsAndR(report.unrealized_pips, report.unrealized_r) : "—",
      hint:
        unit === "dollars" && report?.unrealized_dollars !== null && report
          ? formatDollars(report.unrealized_dollars)
          : undefined,
      tone: report?.unrealized_pips,
    },
    {
      n: "03",
      label: "Max drawdown",
      value: report
        ? formatPipsAndR(-report.max_drawdown_pips, -report.max_drawdown_r)
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
            hint: report ? formatPipsAndR(report.equity_pips, report.realized_r + report.unrealized_r) : undefined,
            tone: report?.equity_dollars ?? undefined,
          },
          ...performanceTiles,
        ]
      : performanceTiles;

  const mix = report?.outcome_mix;
  const marginTiles = [
    {
      n: "06",
      label: "Survivor TP",
      value: report ? formatPct(report.survivor_tp_rate, 1) : "—",
    },
    {
      n: "07",
      label: "Required",
      value: report ? formatPct(report.breakeven_tp_rate_required, 1) : "—",
      hint: report?.mean_loss_r != null ? `mean loss ${report.mean_loss_r.toFixed(2)}R` : undefined,
    },
    {
      n: "08",
      label: "TP-rate margin",
      value: report ? formatPp(report.tp_rate_margin_pp) : "—",
      hint:
        report?.tp_rate_margin_pp_ci_low != null && report.tp_rate_margin_pp_ci_high != null
          ? `${formatPp(report.tp_rate_margin_pp_ci_low)} to ${formatPp(report.tp_rate_margin_pp_ci_high)}`
          : undefined,
      tone: report?.tp_rate_margin_pp ?? undefined,
    },
    {
      n: "09",
      label: "Outcome mix",
      value: mix
        ? `TP ${formatPct(mix.tp)} · lock ${formatPct(mix.lock)}`
        : "—",
      hint: mix
        ? `BE ${formatPct(mix.breakeven)} · whip ${formatPct(mix.whipsaw)}`
        : undefined,
    },
  ];

  return (
    <div>
      <div
        className={cn(
          "grid grid-cols-2 border-b border-border",
          unit === "dollars" ? "md:grid-cols-3 xl:grid-cols-6" : "md:grid-cols-5",
        )}
      >
        {tiles.map((tile) => (
          <KpiTile key={tile.n} {...tile} />
        ))}
      </div>
      <div className="grid grid-cols-2 border-b border-border md:grid-cols-4">
        {marginTiles.map((tile) => (
          <KpiTile key={tile.n} {...tile} />
        ))}
      </div>
    </div>
  );
}

function KpiTile({
  n,
  label,
  value,
  hint,
  tone,
}: {
  n: string;
  label: string;
  value: string;
  hint?: string;
  tone?: number;
}) {
  return (
    <div className="flex min-h-[108px] flex-col justify-between bg-inverted p-4 text-inverted-foreground">
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-inverted-foreground/50">{n}</span>
        <span className="text-[11px] font-medium uppercase">{label}</span>
      </div>
      <div>
        <div
          className={cn(
            "text-sm",
            tone !== undefined && tone > 0 && "text-win",
            tone !== undefined && tone < 0 && "text-loss",
          )}
        >
          {value}
        </div>
        {hint ? (
          <div className="mt-0.5 text-[11px] text-inverted-foreground/50">{hint}</div>
        ) : null}
      </div>
    </div>
  );
}
