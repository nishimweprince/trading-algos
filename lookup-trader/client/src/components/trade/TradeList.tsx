import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ResultBadge } from "@/components/trade/ResultBadge";
import { useTrades } from "@/hooks/useTrades";
import { formatPrice, formatTs } from "@/lib/format";
import type { Session } from "@/types";

interface TradeListProps {
  session: Session | null;
  blinded?: boolean;
}

export function TradeList({ session, blinded }: TradeListProps) {
  const { data: trades = [], isLoading } = useTrades(session?.session_id);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Session Trades ({trades.length})</CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-zinc-500">Loading…</p>}
        {!isLoading && trades.length === 0 && (
          <p className="text-sm text-zinc-500">No trades labelled yet.</p>
        )}
        <div className="space-y-2 max-h-48 overflow-y-auto">
          {trades.map((t) => (
            <div key={t.id} className="rounded border border-zinc-800 p-2 text-xs">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium">{t.setup_id}</span>
                <ResultBadge result={t.result} source={t.source} />
              </div>
              <div className="mt-1 font-mono text-zinc-400">
                {formatTs(t.ts, blinded)} · E {formatPrice(t.entry)} · R {t.realized_r?.toFixed(2) ?? "—"}
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
