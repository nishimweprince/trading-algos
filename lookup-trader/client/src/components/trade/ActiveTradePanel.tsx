import { X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatTradingSession } from "@/lib/tradingSession";
import { useActiveTradeStore } from "@/stores/activeTradeStore";
import { cn } from "@/lib/utils";

const RESULT_STYLES = {
  win: "text-emerald-400 border-emerald-800",
  loss: "text-red-400 border-red-800",
  timeout: "text-amber-400 border-amber-800",
} as const;

export function ActiveTradePanel() {
  const status = useActiveTradeStore((s) => s.status);
  const side = useActiveTradeStore((s) => s.side);
  const setup_id = useActiveTradeStore((s) => s.setup_id);
  const entry = useActiveTradeStore((s) => s.entry);
  const sl = useActiveTradeStore((s) => s.sl);
  const tp = useActiveTradeStore((s) => s.tp);
  const tradingSession = useActiveTradeStore((s) => s.tradingSession);
  const pips = useActiveTradeStore((s) => s.pips);
  const unrealizedR = useActiveTradeStore((s) => s.unrealizedR);
  const barsInTrade = useActiveTradeStore((s) => s.barsInTrade);
  const liveResult = useActiveTradeStore((s) => s.liveResult);
  const exitPrice = useActiveTradeStore((s) => s.exitPrice);
  const cancel = useActiveTradeStore((s) => s.cancel);

  if (status === "idle") return null;

  const resolved = status === "resolved";
  const resultLabel = resolved && liveResult ? liveResult.toUpperCase() : "ACTIVE";
  const resultStyle = resolved && liveResult ? RESULT_STYLES[liveResult] : "text-operator border-operator/40";

  return (
    <Card className="border-operator/30">
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm text-zinc-300">Live trade</CardTitle>
        <div className="flex items-center gap-2">
          <Badge variant="outline" className={cn("tnum font-mono text-[10px]", resultStyle)}>
            {resultLabel}
          </Badge>
          {!resolved && (
            <Button type="button" variant="ghost" size="icon-sm" onClick={cancel} title="Cancel trade">
              <X className="h-3.5 w-3.5" />
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-2 text-xs">
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-zinc-400">
          <span>{setup_id}</span>
          <span>{side === 1 ? "Long" : "Short"}</span>
          <span>{formatTradingSession(tradingSession)}</span>
        </div>
        <div className="grid grid-cols-3 gap-2 tnum font-mono">
          <div>
            <p className="text-zinc-600">Pips</p>
            <p className={cn("text-sm", pips >= 0 ? "text-emerald-400" : "text-red-400")}>
              {pips >= 0 ? "+" : ""}
              {pips.toFixed(1)}
            </p>
          </div>
          <div>
            <p className="text-zinc-600">R</p>
            <p className={cn("text-sm", unrealizedR >= 0 ? "text-emerald-400" : "text-red-400")}>
              {unrealizedR >= 0 ? "+" : ""}
              {unrealizedR.toFixed(2)}
            </p>
          </div>
          <div>
            <p className="text-zinc-600">Bars</p>
            <p className="text-sm text-zinc-200">{barsInTrade}</p>
          </div>
        </div>
        <div className="tnum font-mono text-zinc-500">
          E {entry} · SL {sl} · TP {tp}
          {resolved && exitPrice != null && <> · Exit {exitPrice}</>}
        </div>
      </CardContent>
    </Card>
  );
}
