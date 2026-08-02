import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatCard } from "@/components/common/StatCard";
import { useCompare } from "@/hooks/useCompare";
import { useSetups } from "@/hooks/useSetups";
import { formatPercent } from "@/lib/format";
import type { Session } from "@/types";

interface ComparePanelProps {
  session: Session | null;
}

export function ComparePanel({ session }: ComparePanelProps) {
  const { data: setups = [] } = useSetups();
  const compare = useCompare();
  const [setupId, setSetupId] = useState("");
  const [trendState, setTrendState] = useState("up");
  const [sessionCtx, setSessionCtx] = useState("london");
  const [atrBucket] = useState("mid");
  const [rsiBand] = useState("neutral");

  const runCompare = () => {
    if (!session?.symbol || !session.timeframe || !setupId) return;
    compare.mutate({
      setup_id: setupId,
      symbol: session.symbol,
      timeframe: session.timeframe,
      context: {
        trend_state: trendState,
        session: sessionCtx,
        atr_bucket: atrBucket,
        rsi_band: rsiBand,
      },
    });
  };

  const result = compare.data;

  return (
    <Card>
      <CardHeader><CardTitle>Compare</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-1.5">
          <Label>Setup</Label>
          <Select value={setupId} onValueChange={setSetupId}>
            <SelectTrigger><SelectValue placeholder="Select setup" /></SelectTrigger>
            <SelectContent>
              {setups.map((s) => (
                <SelectItem key={s.setup_id} value={s.setup_id}>{s.name}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1.5">
            <Label>Trend</Label>
            <Select value={trendState} onValueChange={setTrendState}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="up">Up</SelectItem>
                <SelectItem value="down">Down</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>Session</Label>
            <Select value={sessionCtx} onValueChange={setSessionCtx}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="asian">Asian</SelectItem>
                <SelectItem value="london">London</SelectItem>
                <SelectItem value="ny">NY</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <Button onClick={runCompare} disabled={!session || !setupId || compare.isPending} className="w-full">
          {compare.isPending ? "Comparing…" : "Run Compare"}
        </Button>
        {result && (
          <div className="grid grid-cols-2 gap-2">
            <StatCard
              title="Win Rate"
              value={result.level_used === "no_signal" ? "No signal" : formatPercent(result.win_rate)}
              subtitle={`n=${result.decided} · ${result.level_used}`}
            />
            <StatCard
              title="Wilson CI"
              value={
                result.wilson_low != null
                  ? `${formatPercent(result.wilson_low)} – ${formatPercent(result.wilson_high)}`
                  : "—"
              }
            />
            <StatCard title="Expectancy" value={result.expectancy_r?.toFixed(2) ?? "—"} subtitle="in R" />
            <StatCard title="Wins" value={String(result.wins)} subtitle={`${result.timeouts} timeouts`} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
