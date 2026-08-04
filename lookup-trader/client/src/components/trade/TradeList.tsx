import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ResultBadge } from "@/components/trade/ResultBadge";
import { useTrades } from "@/hooks/useTrades";
import { api } from "@/lib/api";
import { formatPrice, formatTs } from "@/lib/format";
import { formatTradingSession, type TradingSession } from "@/lib/tradingSession";
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
        <div className="space-y-2">
          {trades.map((t) => {
            const thumbPath = t.screenshot_entry ?? t.screenshot_exit;
            const thumbUrl = thumbPath ? api.screenshotUrl(thumbPath) : null;
            const sessionLabel =
              t.session && ["asian", "london", "ny", "off_hours"].includes(t.session)
                ? formatTradingSession(t.session as TradingSession)
                : t.session;

            return (
              <div key={t.id} className="rounded border border-zinc-800 p-2 text-xs">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium">{t.setup_id}</span>
                      <ResultBadge result={t.result} source={t.source} />
                    </div>
                    <div className="tnum mt-1 font-mono text-zinc-400">
                      {formatTs(t.ts, blinded)} · E {formatPrice(t.entry)} · R{" "}
                      {t.realized_r?.toFixed(2) ?? "—"}
                      {t.pips_captured != null && (
                        <>
                          {" "}
                          · {t.pips_captured >= 0 ? "+" : ""}
                          {t.pips_captured.toFixed(1)} pips
                        </>
                      )}
                    </div>
                    {sessionLabel && <p className="mt-0.5 text-zinc-500">{sessionLabel} session</p>}
                  </div>
                  {thumbUrl && (
                    <a href={thumbUrl} target="_blank" rel="noreferrer" className="shrink-0">
                      <img
                        src={thumbUrl}
                        alt="Trade chart"
                        className="h-12 w-20 rounded border border-zinc-800 object-cover"
                      />
                    </a>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
