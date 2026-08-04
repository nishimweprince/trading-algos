import { useCallback, useEffect, useRef, useState } from "react";
import { SessionBar } from "@/components/session/SessionBar";
import { ReplaySidebar } from "@/components/session/ReplaySidebar";
import { ReplayChart, type ReplayChartHandle } from "@/components/chart/ReplayChart";
import { ChartViewportState } from "@/components/chart/ChartViewportState";
import { PlaybackControls } from "@/components/controls/PlaybackControls";
import {
  EMPTY_LEVELS,
  type LevelPick,
  type PriceLevelKey,
  type PriceLevels,
} from "@/components/chart/PriceLines";
import { useCandles, useCandleBounds } from "@/hooks/useCandles";
import { useActiveTradeMonitor } from "@/hooks/useActiveTrade";
import { useReplayStore, useVisibleCandles } from "@/hooks/useReplay";
import { useReplayPlayback } from "@/hooks/useReplayPlayback";
import { useReplayKeys } from "@/hooks/useReplayKeys";
import { uploadScreenshot } from "@/lib/screenshot";
import { useActiveTradeStore } from "@/stores/activeTradeStore";
import type { Session } from "@/types";

export function ReplayPage() {
  const [session, setSession] = useState<Session | null>(null);
  const [blinded, setBlinded] = useState(false);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [previewSymbol, setPreviewSymbol] = useState("XAUUSD");
  const [previewTimeframe, setPreviewTimeframe] = useState("H1");
  const [levels, setLevels] = useState<PriceLevels>(EMPTY_LEVELS);
  const [armed, setArmed] = useState<PriceLevelKey | null>(null);
  const [pick, setPick] = useState<LevelPick | null>(null);
  const nonceRef = useRef(0);
  const chartRef = useRef<ReplayChartHandle>(null);

  const setCandles = useReplayStore((s) => s.setCandles);
  const pause = useReplayStore((s) => s.pause);
  const resetReplay = useReplayStore((s) => s.reset);
  const visibleCandles = useVisibleCandles();

  const boundsSymbol = session?.symbol ?? previewSymbol;
  const boundsTimeframe = session?.timeframe ?? previewTimeframe;
  const { data: candleBounds } = useCandleBounds(boundsSymbol, boundsTimeframe);

  const {
    data: candleData,
    isLoading,
    error,
  } = useCandles(session?.symbol ?? "", session?.timeframe ?? "", dateFrom, dateTo, !!session);

  useReplayPlayback();
  useReplayKeys();

  const handleTradeResolved = useCallback(async () => {
    const store = useActiveTradeStore.getState();
    const blob = await chartRef.current?.takeScreenshot();
    if (!blob || !session?.session_id) return;

    try {
      const uploaded = await uploadScreenshot(
        session.session_id,
        "exit",
        blob,
        store.draftTradeId,
      );
      store.setExitScreenshot(blob);
      store.setScreenshotPaths(store.entryScreenshotPath, uploaded.path);
    } catch {
      // Screenshot upload is best-effort; labeling can still proceed.
    }
  }, [session?.session_id]);

  useActiveTradeMonitor(handleTradeResolved);

  useEffect(() => {
    if (candleData) {
      setCandles(candleData);
      pause();
    }
  }, [candleData, setCandles, pause]);

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
    useActiveTradeStore.getState().reset();
    resetReplay();
  };

  const handleInstrumentChange = useCallback((symbol: string, timeframe: string) => {
    setPreviewSymbol(symbol);
    setPreviewTimeframe(timeframe);
  }, []);

  const handleLevelsChange = useCallback((next: PriceLevels) => setLevels(next), []);

  const handlePickPrice = useCallback((field: PriceLevelKey, price: number) => {
    nonceRef.current += 1;
    setPick({ field, price, nonce: nonceRef.current });
    setArmed(null);
  }, []);

  const chartReady = !!session && !isLoading && !error;

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-zinc-950 text-zinc-50">
      <SessionBar
        onSessionStart={handleSessionStart}
        onInstrumentChange={handleInstrumentChange}
        session={session}
        disabled={isLoading}
      />

      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <section className="flex min-h-0 flex-1 flex-col pb-12 lg:min-w-0 lg:pb-0">
          <PlaybackControls blinded={blinded} />
          <ChartViewportState
            session={session}
            isLoading={isLoading && !!session}
            error={error}
            dataRange={candleBounds}
            chartReady={chartReady}
          >
            <ReplayChart
              ref={chartRef}
              candles={visibleCandles}
              blinded={blinded}
              entry={levels.entry}
              sl={levels.sl}
              tp={levels.tp}
              armed={armed}
              onPickPrice={handlePickPrice}
            />
          </ChartViewportState>
        </section>

        <ReplaySidebar
          session={session}
          blinded={blinded}
          levels={levels}
          onLevelsChange={handleLevelsChange}
          armed={armed}
          onArm={setArmed}
          pick={pick}
          chartRef={chartRef}
        />
      </div>
    </div>
  );
}
