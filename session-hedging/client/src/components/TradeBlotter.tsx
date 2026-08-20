import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatPerformance, formatPrice, formatWhen } from "@/lib/format";
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
}

export function TradeBlotter({ pairs, unit }: TradeBlotterProps) {
  if (pairs.length === 0) {
    return (
      <p className="px-1 py-8 text-xs text-muted-foreground">
        No hedge pairs in this city. Run a backtest, or pick another session.
      </p>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Session / open</TableHead>
          <TableHead>Entry</TableHead>
          <TableHead>Primary</TableHead>
          <TableHead>Hedge</TableHead>
          <TableHead>Pair P&amp;L</TableHead>
          <TableHead>Status</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {pairs.map((pair) => {
          const primary = pair.primary ?? pair.unknown_legs[0] ?? null;
          const hedge = pair.hedge ?? pair.unknown_legs[1] ?? null;
          const pairValue = unit === "dollars" ? pair.pnl_dollars : pair.pnl_pips;
          return (
            <TableRow key={pair.id}>
              <TableCell>
                <div>{SESSION_LABEL[pair.session] ?? pair.session}</div>
                <div className="text-[11px] text-muted-foreground">
                  {formatWhen(pair.entry_ts)}
                </div>
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
                {formatPerformance(pair.pnl_pips, pair.pnl_dollars, unit)}
              </TableCell>
              <TableCell>
                <Badge variant="outline">{pair.status}</Badge>
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
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
  const value = unit === "dollars" ? leg.pnl_dollars : leg.pnl_pips;
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
        {formatPerformance(leg.pnl_pips, leg.pnl_dollars, unit)}
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
