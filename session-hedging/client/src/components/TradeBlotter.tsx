import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatMoney, formatPrice, formatWhen } from "@/lib/format";
import { SESSION_LABEL, type ClosedLeg } from "@/lib/types";
import { cn } from "@/lib/utils";

interface TradeBlotterProps {
  trades: ClosedLeg[];
}

export function TradeBlotter({ trades }: TradeBlotterProps) {
  if (trades.length === 0) {
    return (
      <p className="px-1 py-8 text-xs text-muted-foreground">
        No closed legs in this city. Run a backtest, or pick another session.
      </p>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Session</TableHead>
          <TableHead>Side</TableHead>
          <TableHead>Entry</TableHead>
          <TableHead>Exit</TableHead>
          <TableHead>PnL</TableHead>
          <TableHead>Result</TableHead>
          <TableHead>When</TableHead>
          <TableHead>Reason</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {trades.map((trade, index) => (
          <TableRow key={`${trade.ts}-${trade.side}-${index}`}>
            <TableCell>{SESSION_LABEL[trade.session] ?? trade.session}</TableCell>
            <TableCell>
              <Badge variant={trade.side}>{trade.side}</Badge>
            </TableCell>
            <TableCell>{formatPrice(trade.entry)}</TableCell>
            <TableCell>{formatPrice(trade.exit)}</TableCell>
            <TableCell
              className={cn(
                trade.pnl > 0 && "text-win",
                trade.pnl < 0 && "text-loss",
              )}
            >
              {formatMoney(trade.pnl)}
            </TableCell>
            <TableCell>
              <Badge variant={trade.bucket}>{trade.bucket}</Badge>
            </TableCell>
            <TableCell className="text-[11px] text-muted-foreground">
              {formatWhen(trade.ts)}
            </TableCell>
            <TableCell className="text-xs text-muted-foreground">{trade.reason}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
