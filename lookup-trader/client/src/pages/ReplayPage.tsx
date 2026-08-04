import { useCallback, useEffect, useRef, useState } from "react";
import { useWatch } from "react-hook-form";
import { SessionBar } from "@/components/session/SessionBar";
import { ReplaySidebar } from "@/components/session/ReplaySidebar";
import { ReplayChart, type ReplayChartHandle } from "@/components/chart/ReplayChart";
import { ChartViewportState } from "@/components/chart/ChartViewportState";
import { PlaybackControls } from "@/components/controls/PlaybackControls";
import { Form } from "@/components/ui/form";
import { type PriceLevelKey } from "@/components/chart/PriceLines";
import { useCandles, useCandleBounds } from "@/hooks/useCandles";
import { useActiveTradeMonitor } from "@/hooks/useActiveTrade";
import { EMPTY_MARK_TRADE, toLevel, useMarkTradeForm } from "@/hooks/useMarkTradeForm";
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
  // Which level the next chart click sets. Transient UI, never submitted.
  const [armed, setArmed] = useState<PriceLevelKey | null>(null);
  const chartRef = useRef<ReplayChartHandle>(null);

  // One form for the whole page: the chart writes into it, the chart reads
  // levels out of it, and the sidebar edits it without owning it — so switching
  // sidebar tabs (which unmounts TradeForm) no longer loses the marked trade.
  const markForm = useMarkTradeForm();
  const [entry, sl, tp] = useWatch({
    control: markForm.control,
    name: ["entry", "sl", "tp"],
  });

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
    markForm.reset(EMPTY_MARK_TRADE);
    setArmed(null);
    useActiveTradeStore.getState().reset();
    resetReplay();
  };

  const handleInstrumentChange = useCallback((symbol: string, timeframe: string) => {
    setPreviewSymbol(symbol);
    setPreviewTimeframe(timeframe);
  }, []);

  // Straight into the form. The old path routed through a `pick` state object
  // carrying a nonce, purely so that re-picking the same price on the same field
  // still registered as a change — setValue has no such problem.
  const handlePickPrice = useCallback(
    (field: PriceLevelKey, price: number) => {
      markForm.setValue(field, String(price), { shouldValidate: true, shouldDirty: true });
      useActiveTradeStore.getState().noteLevelRevision();
      setArmed(null);
    },
    [markForm],
  );

  const chartReady = !!session && !isLoading && !error;

  return (
    <Form {...markForm}>
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
                entry={toLevel(entry)}
                sl={toLevel(sl)}
                tp={toLevel(tp)}
                armed={armed}
                onPickPrice={handlePickPrice}
              />
            </ChartViewportState>
          </section>

          <ReplaySidebar
            session={session}
            blinded={blinded}
            armed={armed}
            onArm={setArmed}
            chartRef={chartRef}
          />
        </div>
      </div>
    </Form>
  );
}
