import { useCallback, useEffect, useRef, useState } from "react";
import { SessionBar } from "@/components/session/SessionBar";
import { ReplayChart } from "@/components/chart/ReplayChart";
import { PlaybackControls } from "@/components/controls/PlaybackControls";
import { TradeForm } from "@/components/trade/TradeForm";
import { TradeList } from "@/components/trade/TradeList";
import { ComparePanel } from "@/components/trade/ComparePanel";
import {
  EMPTY_LEVELS,
  type LevelPick,
  type PriceLevelKey,
  type PriceLevels,
} from "@/components/chart/PriceLines";
import { useCandles } from "@/hooks/useCandles";
import { useReplayStore, useVisibleCandles } from "@/hooks/useReplay";
import { useReplayPlayback } from "@/hooks/useReplayPlayback";
import { useReplayKeys } from "@/hooks/useReplayKeys";
import type { Session } from "@/types";

export function ReplayPage() {
  const [session, setSession] = useState<Session | null>(null);
  const [blinded, setBlinded] = useState(false);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [levels, setLevels] = useState<PriceLevels>(EMPTY_LEVELS);
  const [armed, setArmed] = useState<PriceLevelKey | null>(null);
  const [pick, setPick] = useState<LevelPick | null>(null);
  const nonceRef = useRef(0);

  const setCandles = useReplayStore((s) => s.setCandles);
  const pause = useReplayStore((s) => s.pause);
  const visibleCandles = useVisibleCandles();

  const {
    data: candleData,
    isLoading,
    error,
  } = useCandles(session?.symbol ?? "", session?.timeframe ?? "", dateFrom, dateTo, !!session);

  useReplayPlayback();
  useReplayKeys();

  useEffect(() => {
    if (candleData) {
      setCandles(candleData);
      pause();
    }
  }, [candleData, setCandles, pause]);

  // Escape backs out of level-picking. The transport shortcuts leave it alone.
  useEffect(() => {
    if (!armed) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setArmed(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [armed]);

  const handleSessionStart = (
    s: Session,
    isBlinded: boolean,
    range: { date_from: string; date_to: string },
  ) => {
    setSession(s);
    setBlinded(isBlinded);
    setDateFrom(range.date_from);
    setDateTo(range.date_to);
    setLevels(EMPTY_LEVELS);
    setArmed(null);
    setPick(null);
  };

  // Stable identity so TradeForm's watch subscription isn't torn down each render.
  const handleLevelsChange = useCallback((next: PriceLevels) => setLevels(next), []);

  // One click places one level; the form is what actually holds the value.
  const handlePickPrice = useCallback((field: PriceLevelKey, price: number) => {
    nonceRef.current += 1;
    setPick({ field, price, nonce: nonceRef.current });
    setArmed(null);
  }, []);

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-zinc-950 text-zinc-50">
      <header className="flex items-baseline gap-3 border-b border-zinc-800 px-4 py-2.5">
        <h1 className="text-sm font-medium">Lookup Trader</h1>
        <span className="text-xs text-zinc-400">Bar replay</span>
        <p className="ml-auto hidden text-xs text-zinc-600 sm:block">
          Manual labelling · no future bars revealed
        </p>
      </header>

      <SessionBar onSessionStart={handleSessionStart} session={session} disabled={isLoading} />

      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        {/* Transport sits above the chart: it is the control the operator reaches
            for on every single bar. */}
        <section className="flex min-h-0 flex-1 flex-col lg:min-w-0">
          <PlaybackControls />
          <div className="relative min-h-[320px] flex-1 p-2">
            {isLoading && (
              <div className="absolute inset-0 flex items-center justify-center text-sm text-zinc-500">
                Loading candles…
              </div>
            )}
            {error && (
              <div className="absolute inset-0 flex items-center justify-center px-6 text-center text-sm text-[var(--color-destructive)]">
                {error.message}
              </div>
            )}
            {!session && !isLoading && (
              <div className="absolute inset-0 flex items-center justify-center text-sm text-zinc-500">
                Pick a symbol and a window above, then start a session.
              </div>
            )}
            {session && !isLoading && !error && (
              <ReplayChart
                candles={visibleCandles}
                blinded={blinded}
                entry={levels.entry}
                sl={levels.sl}
                tp={levels.tp}
                armed={armed}
                onPickPrice={handlePickPrice}
              />
            )}
          </div>
        </section>

        {/* One sidebar column below xl; two beside the chart above it, so the
            compare stats stay on screen instead of below the fold. */}
        <aside className="flex w-full shrink-0 flex-col gap-3 overflow-y-auto border-t border-zinc-800 p-3 lg:w-[22rem] lg:border-l lg:border-t-0 xl:w-[40rem] xl:flex-row xl:items-start">
          <div className="flex min-w-0 flex-col gap-3 xl:flex-1">
            <TradeForm
              session={session}
              blinded={blinded}
              levels={levels}
              onLevelsChange={handleLevelsChange}
              dateFrom={dateFrom}
              dateTo={dateTo}
              armed={armed}
              onArm={setArmed}
              pick={pick}
            />
          </div>
          <div className="flex min-w-0 flex-col gap-3 xl:flex-1">
            <TradeList session={session} blinded={blinded} />
            <ComparePanel session={session} />
          </div>
        </aside>
      </div>
    </div>
  );
}
