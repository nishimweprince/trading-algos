import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useSymbols, useTimeframes } from "@/hooks/useCandles";
import { toIsoEnd, toIsoStart } from "@/lib/format";
import { api } from "@/lib/api";
import type { Session } from "@/types";

const TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];

interface SessionBarProps {
  onSessionStart: (session: Session, blinded: boolean) => void;
  disabled?: boolean;
}

export function SessionBar({ onSessionStart, disabled }: SessionBarProps) {
  const { data: symbols = [] } = useSymbols();
  const [symbol, setSymbol] = useState("EURUSD");
  const [timeframe, setTimeframe] = useState("H1");
  const [dateFrom, setDateFrom] = useState("2024-01-01");
  const [dateTo, setDateTo] = useState("2024-01-31");
  const [blinded, setBlinded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: timeframes = [] } = useTimeframes(symbol);
  const availableTimeframes = timeframes.length > 0 ? timeframes : TIMEFRAMES;
  const availableSymbols = symbols.length > 0 ? symbols : ["EURUSD"];

  const handleStart = async () => {
    setLoading(true);
    setError(null);
    try {
      const session = await api.createSession({
        symbol,
        timeframe,
        date_from: toIsoStart(dateFrom),
        date_to: toIsoEnd(dateTo),
        blinded,
      });
      onSessionStart(session, blinded);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start session");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex flex-wrap items-end gap-4 border-b border-zinc-800 bg-zinc-950 p-4">
      <div className="space-y-1.5">
        <Label>Symbol</Label>
        <Select value={symbol} onValueChange={setSymbol}>
          <SelectTrigger className="w-32">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {availableSymbols.map((s) => (
              <SelectItem key={s} value={s}>{s}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="space-y-1.5">
        <Label>Timeframe</Label>
        <Select value={timeframe} onValueChange={setTimeframe}>
          <SelectTrigger className="w-24">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {availableTimeframes.map((tf) => (
              <SelectItem key={tf} value={tf}>{tf}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="space-y-1.5">
        <Label>{blinded ? "From" : "Date from"}</Label>
        <Input type="date" value={blinded ? "" : dateFrom} onChange={(e) => setDateFrom(e.target.value)} placeholder={blinded ? "•••" : undefined} />
      </div>
      <div className="space-y-1.5">
        <Label>{blinded ? "To" : "Date to"}</Label>
        <Input type="date" value={blinded ? "" : dateTo} onChange={(e) => setDateTo(e.target.value)} placeholder={blinded ? "•••" : undefined} />
      </div>
      <div className="flex items-center gap-2 pb-2">
        <Switch id="blinded" checked={blinded} onCheckedChange={setBlinded} />
        <Label htmlFor="blinded">Blinded</Label>
      </div>
      <Button onClick={handleStart} disabled={disabled || loading}>
        {loading ? "Starting…" : "Start Session"}
      </Button>
      {error && <span className="text-sm text-red-400">{error}</span>}
    </div>
  );
}
