import { faArrowUpRightFromSquare } from "@fortawesome/free-solid-svg-icons";
import { Icon } from "@/lib/icon";
import { formatDollars, formatPips } from "@/lib/format";
import { pairSessionBreakdown } from "@/lib/stats";
import {
  SESSION_LABEL,
  SESSIONS,
  type PerformanceUnit,
  type SessionAnchorStats,
  type TradePairResult,
} from "@/lib/types";
import { cn } from "@/lib/utils";

interface SessionRailProps {
  active: string | null;
  present: string[];
  pairs: TradePairResult[];
  unit: PerformanceUnit;
  anchorStats?: SessionAnchorStats[];
  onSelect: (session: string | null) => void;
}

export function SessionRail({
  active,
  present,
  pairs,
  unit,
  anchorStats = [],
  onSelect,
}: SessionRailProps) {
  const breakdown = pairSessionBreakdown(pairs, unit);
  return (
    <div className="grid grid-cols-1 border-b border-border md:grid-cols-3" role="tablist" aria-label="Filter by session">
      {SESSIONS.map((name) => {
        const selected = active === name;
        const inRun = present.length === 0 || present.includes(name);
        const row = breakdown.find((item) => item.session === name);
        const drift = anchorStats.find((item) => item.session === name);
        const p50 = drift?.anchor_drift_p50;
        return (
          <button
            key={name}
            type="button"
            role="tab"
            aria-selected={selected}
            disabled={!inRun}
            onClick={() => onSelect(selected ? null : name)}
            className={cn(
              "relative flex min-h-[96px] cursor-pointer flex-col items-start justify-between p-4 text-left transition-colors",
              "border-b border-border last:border-b-0 md:border-b-0 md:border-r md:last:border-r-0",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
              "disabled:cursor-not-allowed disabled:opacity-40",
              selected
                ? "bg-inverted text-inverted-foreground"
                : "bg-background text-foreground hover:bg-accent",
            )}
          >
            <span
              className={cn(
                "text-[11px] uppercase tracking-[0.18em]",
                selected ? "text-inverted-foreground/50" : "text-muted-foreground",
              )}
            >
              Session
            </span>
            <div className="flex w-full items-end justify-between gap-3">
              <div>
                <div className="text-sm font-medium">{SESSION_LABEL[name]}</div>
                <div
                  className={cn(
                    "mt-0.5 text-xs",
                    row && row.pnl < 0 && "text-loss",
                    row && row.pnl > 0 && "text-win",
                  )}
                >
                  {row
                    ? unit === "dollars"
                      ? formatDollars(row.pnl)
                      : formatPips(row.pnl)
                    : "No legs yet"}
                </div>
                <div
                  className={cn(
                    "mt-0.5 text-[11px]",
                    selected ? "text-inverted-foreground/50" : "text-muted-foreground",
                  )}
                >
                  {p50 == null ? "drift p50 —" : `drift p50 ${p50.toFixed(0)}m`}
                </div>
              </div>
              <Icon
                icon={faArrowUpRightFromSquare}
                className={cn("h-3 w-3", selected ? "opacity-80" : "opacity-50")}
              />
            </div>
          </button>
        );
      })}
    </div>
  );
}
