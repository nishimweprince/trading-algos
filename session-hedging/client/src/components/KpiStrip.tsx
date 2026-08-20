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
  const grossRealizedPips = report?.gross_realized_pips ?? report?.realized_pips ?? 0;
  const grossRealizedR = report?.gross_realized_r ?? report?.realized_r ?? 0;
  const netRealizedPips = report?.net_realized_pips ?? grossRealizedPips;
  const netRealizedR = report?.net_realized_r ?? grossRealizedR;
  const grossOpenPips = report?.gross_unrealized_pips ?? report?.unrealized_pips ?? 0;
  const grossOpenR = report?.gross_unrealized_r ?? report?.unrealized_r ?? 0;
  const netOpenPips = report?.net_unrealized_pips ?? grossOpenPips;
  const netOpenR = report?.net_unrealized_r ?? grossOpenR;

  const performanceTiles = [
    {
      n: "01",
      label: "Realized gross",
      value: report ? formatPipsAndR(grossRealizedPips, grossRealizedR) : "—",
      hint:
        report
          ? `net ${formatPipsAndR(netRealizedPips, netRealizedR)} · cost ${(report.realized_cost_pips ?? 0).toFixed(1)}p`
          : undefined,
      tone: report ? netRealizedPips : undefined,
    },
    {
      n: "02",
      label: "Open gross",
      value: report ? formatPipsAndR(grossOpenPips, grossOpenR) : "—",
      hint:
        report
          ? `net ${formatPipsAndR(netOpenPips, netOpenR)} · cost ${(report.unrealized_cost_pips ?? 0).toFixed(1)}p`
          : undefined,
      tone: report ? netOpenPips : undefined,
    },
    {
      n: "03",
      label: "Max drawdown",
      value: report
        ? formatPipsAndR(
            -(report.gross_max_drawdown_pips ?? report.max_drawdown_pips),
            -(report.gross_max_drawdown_r ?? report.max_drawdown_r),
          )
        : "—",
      hint: report
        ? `net ${formatPipsAndR(
            -(report.net_max_drawdown_pips ?? report.max_drawdown_pips),
            -(report.net_max_drawdown_r ?? report.max_drawdown_r),
          )}`
        : undefined,
      tone:
        report && (report.net_max_drawdown_pips ?? report.max_drawdown_pips) > 0
          ? -1
          : undefined,
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
        ? `BE ${formatPct(mix.breakeven)} · whip ${formatPct(mix.whipsaw)} · time ${formatPct(mix.time_exit ?? 0)}`
        : undefined,
    },
    {
      n: "10",
      label: "Cost headroom",
      value:
        report?.breakeven_pips_per_side != null
          ? `${report.breakeven_pips_per_side.toFixed(1)}p / side`
          : "—",
      hint:
        report?.cost_headroom_ratio != null
          ? `${report.cost_headroom_ratio.toFixed(2)}× configured spread`
          : report
            ? "spread is zero"
            : undefined,
      tone:
        report?.cost_headroom_ratio != null ? report.cost_headroom_ratio - 2 : undefined,
    },
    {
      n: "11",
      label: "Concurrency",
      value: report ? String(report.max_concurrent_structures) : "—",
      hint: report
        ? `${report.suppressed_signal_count ?? 0} suppressed · guard ${report.prop_guard_breached ? "TRIPPED" : "clear"}`
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
      <div className="grid grid-cols-2 border-b border-border md:grid-cols-6">
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
