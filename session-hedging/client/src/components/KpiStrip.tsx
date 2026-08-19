import { formatMoney, formatPct } from "@/lib/format";
import { closedCount, winRate } from "@/lib/stats";
import type { BacktestReport } from "@/lib/types";
import { cn } from "@/lib/utils";

interface KpiStripProps {
  report: BacktestReport | null;
}

export function KpiStrip({ report }: KpiStripProps) {
  const longN = report ? closedCount(report.long_wins, report.long_be, report.long_loss) : 0;
  const shortN = report ? closedCount(report.short_wins, report.short_be, report.short_loss) : 0;
  const combined = report
    ? winRate(
        report.long_wins + report.short_wins,
        report.long_be + report.short_be,
        report.long_loss + report.short_loss,
      )
    : null;

  const tiles = [
    {
      n: "01",
      label: "Equity",
      value: report ? formatMoney(report.equity) : "—",
      tone: report?.equity,
    },
    {
      n: "02",
      label: "Realized",
      value: report ? formatMoney(report.realized) : "—",
      tone: report?.realized,
    },
    {
      n: "03",
      label: "Locks",
      value: report ? String(report.locks) : "—",
    },
    {
      n: "04",
      label: "Win rate",
      value: report ? formatPct(combined) : "—",
      hint: report ? `${longN + shortN} closed` : undefined,
    },
  ];

  return (
    <div className="grid grid-cols-2 border-b border-border md:grid-cols-4">
      {tiles.map((tile) => (
        <div
          key={tile.n}
          className="flex min-h-[108px] flex-col justify-between bg-inverted p-4 text-inverted-foreground"
        >
          <span className="text-[11px] text-inverted-foreground/50">{tile.n}</span>
          <div className="flex items-end justify-between gap-2">
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
                <div className="text-[11px] text-inverted-foreground/50">{tile.hint}</div>
              ) : null}
            </div>
            <span className="text-xs font-medium">{tile.label}</span>
          </div>
        </div>
      ))}
    </div>
  );
}
