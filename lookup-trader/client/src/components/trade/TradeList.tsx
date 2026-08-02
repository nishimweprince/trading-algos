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
      <CardHeader className="flex-row items-baseline justify-between gap-2 space-y-0">
        <CardTitle className="text-zinc-400">Session trades</CardTitle>
        <span className="tnum font-mono text-xs text-zinc-500">{trades.length}</span>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-zinc-500">Loading…</p>}
        {!isLoading && trades.length === 0 && (
          <p className="text-sm text-zinc-500">
            {session ? "Mark a trade to start the record." : "Start a session to label trades."}
          </p>
        )}
        {/* No inner scroller: the sidebar is the one scroll region, so nothing
            past the third trade hides inside a nested box. */}
        <div className="space-y-2">
          {trades.map((t) => (
            <div key={t.id} className="rounded border border-zinc-800 p-2 text-xs">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium">{t.setup_id}</span>
                <ResultBadge result={t.result} source={t.source} />
              </div>
              <div className="tnum mt-1 font-mono text-zinc-400">
                {formatTs(t.ts, blinded)} · E {formatPrice(t.entry)} · R {t.realized_r?.toFixed(2) ?? "—"}
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
