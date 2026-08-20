import { useMemo, useState } from "react";
import { faSort, faSortDown, faSortUp } from "@fortawesome/free-solid-svg-icons";
import { TradePairDetailDialog } from "@/components/TradePairDetailDialog";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { BacktestCsvContext } from "@/lib/csv";
import { formatPerformance, formatPrice, formatWhen } from "@/lib/format";
import { Icon } from "@/lib/icon";
import { sortPairs, type PairSortKey, type SortDir } from "@/lib/stats";
import {
  SESSION_LABEL,
  type PerformanceUnit,
  type TradePairLeg,
  type TradePairResult,
} from "@/lib/types";
import { cn } from "@/lib/utils";

interface TradeBlotterProps {
  pairs: TradePairResult[];
  unit: PerformanceUnit;
  context: BacktestCsvContext | null;
}

const DEFAULT_SORT_KEY: PairSortKey = "entry_ts";
const DEFAULT_SORT_DIR: SortDir = "desc";

export function TradeBlotter({ pairs, unit, context }: TradeBlotterProps) {
  const [sortKey, setSortKey] = useState<PairSortKey>(DEFAULT_SORT_KEY);
  const [sortDir, setSortDir] = useState<SortDir>(DEFAULT_SORT_DIR);
  const [selectedPair, setSelectedPair] = useState<TradePairResult | null>(null);
  const ordered = useMemo(
    () => sortPairs(pairs, sortKey, sortDir, unit),
    [pairs, sortKey, sortDir, unit],
  );

  function toggleSort(key: PairSortKey) {
    if (key === sortKey) {
      setSortDir((current) => (current === "desc" ? "asc" : "desc"));
      return;
    }
    setSortKey(key);
    setSortDir(key === "session" || key === "status" ? "asc" : "desc");
  }

  if (pairs.length === 0) {
    return (
      <p className="px-1 py-8 text-xs text-muted-foreground">
        No hedge pairs in this city. Run a backtest, or pick another session.
      </p>
    );
  }

  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <SortHead
              label="Session / open"
              column="entry_ts"
              activeKey={sortKey}
              dir={sortDir}
              onSort={toggleSort}
            />
            <SortHead
              label="Entry"
              column="entry"
              activeKey={sortKey}
              dir={sortDir}
              onSort={toggleSort}
            />
            <TableHead>Primary</TableHead>
            <TableHead>Hedge</TableHead>
            <SortHead
              label="Pair P&L"
              column="pnl"
              activeKey={sortKey}
              dir={sortDir}
              onSort={toggleSort}
            />
            <SortHead
              label="Status"
              column="status"
              activeKey={sortKey}
              dir={sortDir}
              onSort={toggleSort}
            />
          </TableRow>
        </TableHeader>
        <TableBody>
          {ordered.map((pair) => {
            const primary = pair.primary ?? pair.unknown_legs[0] ?? null;
            const hedge = pair.hedge ?? pair.unknown_legs[1] ?? null;
            const grossPairPips = pair.gross_pnl_pips ?? pair.pnl_pips;
            const netPairPips = pair.net_pnl_pips ?? grossPairPips;
            const pairValue = unit === "dollars" ? pair.pnl_dollars : netPairPips;
            return (
              <TableRow
                key={pair.id}
                className={cn(
                  context && "cursor-pointer hover:bg-accent/50",
                  selectedPair?.id === pair.id && "bg-accent/40",
                )}
                tabIndex={context ? 0 : undefined}
                onClick={context ? () => setSelectedPair(pair) : undefined}
                onKeyDown={
                  context
                    ? (event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setSelectedPair(pair);
                        }
                      }
                    : undefined
                }
                aria-label={context ? `View details for ${pair.id}` : undefined}
              >
                <TableCell>
                  <div>{SESSION_LABEL[pair.session] ?? pair.session}</div>
                  <div className="text-[11px] text-muted-foreground">{formatWhen(pair.entry_ts)}</div>
                </TableCell>
                <TableCell>{formatPrice(pair.entry)}</TableCell>
                <TableCell>
                  <LegResult leg={primary} unit={unit} fallbackRole="primary" />
                </TableCell>
                <TableCell>
                  <LegResult leg={hedge} unit={unit} fallbackRole="hedge" />
                </TableCell>
                <TableCell
                  className={cn(
                    pairValue !== null && pairValue > 0 && "text-win",
                    pairValue !== null && pairValue < 0 && "text-loss",
                  )}
                >
                  {unit === "pips" ? (
                    <div>
                      <div>G {formatPerformance(grossPairPips, null, unit)}</div>
                      <div className="text-[11px] text-muted-foreground">
                        N {formatPerformance(netPairPips, null, unit)}
                      </div>
                    </div>
                  ) : (
                    formatPerformance(pair.pnl_pips, pair.pnl_dollars, unit)
                  )}
                </TableCell>
                <TableCell>
                  <Badge variant="outline">{pair.status}</Badge>
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>

      <TradePairDetailDialog
        open={selectedPair !== null}
        pair={selectedPair}
        context={context}
        onClose={() => setSelectedPair(null)}
      />
    </>
  );
}

function SortHead({
  label,
  column,
  activeKey,
  dir,
  onSort,
}: {
  label: string;
  column: PairSortKey;
  activeKey: PairSortKey;
  dir: SortDir;
  onSort: (key: PairSortKey) => void;
}) {
  const active = activeKey === column;
  const icon = !active ? faSort : dir === "desc" ? faSortDown : faSortUp;
  return (
    <TableHead>
      <button
        type="button"
        onClick={() => onSort(column)}
        className="inline-flex cursor-pointer items-center gap-1.5 uppercase hover:text-foreground"
        aria-label={`Sort by ${label}${active ? `, ${dir === "desc" ? "latest first" : "oldest first"}` : ""}`}
      >
        {label}
        <Icon icon={icon} className={cn("h-2.5 w-2.5", active ? "opacity-100" : "opacity-40")} />
      </button>
    </TableHead>
  );
}

function LegResult({
  leg,
  unit,
  fallbackRole,
}: {
  leg: TradePairLeg | null;
  unit: PerformanceUnit;
  fallbackRole: "primary" | "hedge";
}) {
  if (!leg) return <span className="text-muted-foreground">Unknown</span>;
  const grossPips = leg.gross_pnl_pips ?? leg.pnl_pips;
  const netPips = leg.net_pnl_pips ?? grossPips;
  const value = unit === "dollars" ? leg.pnl_dollars : netPips;
  return (
    <div className="min-w-[150px] space-y-1">
      <div className="flex items-center gap-1.5">
        <Badge variant={leg.side}>{leg.side}</Badge>
        <span className="text-[10px] uppercase text-muted-foreground">
          {leg.role === "unknown" ? "unknown role" : fallbackRole}
        </span>
        {leg.bucket ? <Badge variant={leg.bucket}>{leg.bucket}</Badge> : null}
      </div>
      <div
        className={cn(
          "text-xs",
          value !== null && value > 0 && "text-win",
          value !== null && value < 0 && "text-loss",
        )}
      >
        {unit === "pips" ? (
          <>
            G {formatPerformance(grossPips, null, unit)} · N{" "}
            {formatPerformance(netPips, null, unit)}
          </>
        ) : (
          formatPerformance(leg.pnl_pips, leg.pnl_dollars, unit)
        )}
      </div>
      <div className="text-[11px] text-muted-foreground">
        {leg.status === "open"
          ? "Open at final close"
          : `${formatPrice(leg.exit ?? 0)} · ${formatWhen(leg.exit_ts ?? "")}`}
      </div>
      {leg.reason ? <div className="text-[10px] text-muted-foreground">{leg.reason}</div> : null}
    </div>
  );
}
