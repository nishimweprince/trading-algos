import { useEffect } from "react";
import { AMBIGUOUS_POLICY, MAX_BARS } from "@/lib/constants";
import {
  checkBar,
  exitPriceFromResult,
  resolveHit,
} from "@/lib/barrier";
import { pipSize, pipsCaptured } from "@/lib/pips";
import { getCandleAt, useReplayStore } from "@/hooks/useReplay";
import { useActiveTradeStore } from "@/stores/activeTradeStore";

/** Monitors cursor advances and resolves active trades on TP/SL/timeout. */
export function useActiveTradeMonitor(onResolved?: () => void) {
  const cursor = useReplayStore((s) => s.cursor);
  const candles = useReplayStore((s) => s.candles);
  const loadedStartOrdinal = useReplayStore((s) => s.loadedStartOrdinal);
  const pause = useReplayStore((s) => s.pause);
  const status = useActiveTradeStore((s) => s.status);
  const signalIdx = useActiveTradeStore((s) => s.signalIdx);
  const lastCheckedIdx = useActiveTradeStore((s) => s.lastCheckedIdx);

  useEffect(() => {
    if (status !== "active") return;
    if (cursor <= lastCheckedIdx) return;

    const state = useActiveTradeStore.getState();
    const size = pipSize(state.symbol);

    for (let i = Math.max(signalIdx + 1, lastCheckedIdx + 1); i <= cursor; i++) {
      const bar = getCandleAt({ candles, loadedStartOrdinal }, i);
      if (!bar) continue;

      const hit = checkBar(bar, { side: state.side, sl: state.sl, tp: state.tp });
      if (hit) {
        const result = resolveHit(hit, AMBIGUOUS_POLICY);
        if (result) {
          const exitPrice = exitPriceFromResult(
            result,
            state.side,
            state.entry,
            state.sl,
            state.tp,
            bar,
          );
          const pips = pipsCaptured(state.side, state.entry, exitPrice, size);
          state.resolveTrade(result, i, exitPrice, pips);
          state.setLastCheckedIdx(i);
          pause();
          onResolved?.();
          return;
        }
      }

      if (i - signalIdx >= MAX_BARS) {
        const exitPrice = bar.close;
        const pips = pipsCaptured(state.side, state.entry, exitPrice, size);
        state.resolveTrade("timeout", i, exitPrice, pips);
        state.setLastCheckedIdx(i);
        pause();
        onResolved?.();
        return;
      }
    }

    const currentBar = getCandleAt({ candles, loadedStartOrdinal }, cursor);
    if (currentBar) {
      state.updateLive(cursor, currentBar, size);
    }
    state.setLastCheckedIdx(cursor);
  }, [cursor, candles, loadedStartOrdinal, status, signalIdx, lastCheckedIdx, pause, onResolved]);
}

export { useActiveTradeStore } from "@/stores/activeTradeStore";
export type { StartTradeParams, TradeStatus } from "@/stores/activeTradeStore";
