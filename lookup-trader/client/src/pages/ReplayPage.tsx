import { useEffect, useState } from "react";
import { SessionBar } from "@/components/session/SessionBar";
import { ReplayChart } from "@/components/chart/ReplayChart";
import { PlaybackControls } from "@/components/controls/PlaybackControls";
import { TradeForm } from "@/components/trade/TradeForm";
import { TradeList } from "@/components/trade/TradeList";
import { ComparePanel } from "@/components/trade/ComparePanel";
import { EMPTY_LEVELS, type PriceLevels } from "@/components/chart/PriceLines";
import { useCandles } from "@/hooks/useCandles";
import { useReplayStore, useVisibleCandles } from "@/hooks/useReplay";
import { useReplayPlayback } from "@/hooks/useReplayPlayback";
import type { Session } from "@/types";

export function ReplayPage() {
  const [session, setSession] = useState<Session | null>(null);
  const [blinded, setBlinded] = useState(false);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [levels, setLevels] = useState<PriceLevels>(EMPTY_LEVELS);

  const setCandles = useReplayStore((s) => s.setCandles);
  const pause = useReplayStore((s) => s.pause);
  const visibleCandles = useVisibleCandles();

  const { data: candleData, isLoading, error } = useCandles(
    session?.symbol ?? "",
    session?.timeframe ?? "",
    dateFrom,
    dateTo,
    !!session,
  );

  useReplayPlayback();

  useEffect(() => {
    if (candleData) {
      setCandles(candleData);
      pause();
    }
  }, [candleData, setCandles, pause]);

  const handleSessionStart = (s: Session, isBlinded: boolean) => {
    setSession(s);
    setBlinded(isBlinded);
    setDateFrom(s.date_from ?? "");
    setDateTo(s.date_to ?? "");
    setLevels(EMPTY_LEVELS);
  };

  return (
    <div className="flex min-h-screen flex-col bg-zinc-950 text-zinc-50">
      <header className="border-b border-zinc-800 px-4 py-3">
        <h1 className="text-lg font-semibold tracking-tight">Lookup Trader — Bar Replay</h1>
        <p className="text-xs text-zinc-500">Manual labelling · no future bars revealed</p>
      </header>

      <SessionBar onSessionStart={handleSessionStart} disabled={!!session && isLoading} />

      <div className="flex flex-1 flex-col lg:flex-row min-h-0">
        <div className="flex flex-1 flex-col min-h-0 lg:w-[70%]">
          <div className="relative flex-1 min-h-[400px] p-2">
            {isLoading && <div className="absolute inset-0 flex items-center justify-center text-zinc-500">Loading candles…</div>}
            {error && <div className="absolute inset-0 flex items-center justify-center text-red-400">{error.message}</div>}
            {!session && !isLoading && (
              <div className="absolute inset-0 flex items-center justify-center text-zinc-500">Start a session to begin replay</div>
            )}
            {session && <ReplayChart candles={visibleCandles} blinded={blinded} entry={levels.entry} sl={levels.sl} tp={levels.tp} />}
          </div>
          <PlaybackControls />
        </div>

        <aside className="flex w-full flex-col gap-3 overflow-y-auto border-t border-zinc-800 p-3 lg:w-[30%] lg:border-t-0 lg:border-l">
          <TradeForm
            session={session}
            blinded={blinded}
            levels={levels}
            onLevelsChange={setLevels}
            dateFrom={dateFrom}
            dateTo={dateTo}
          />
          <TradeList session={session} blinded={blinded} />
          <ComparePanel session={session} />
        </aside>
      </div>
    </div>
  );
}
