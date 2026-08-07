import { beforeEach, describe, expect, it } from "vitest";
import { CANDLE_PAGE_SIZE, getCandleAt, selectVisibleCandles, useReplayStore } from "./useReplay";
import { useActiveTradeStore } from "@/stores/activeTradeStore";
import type { CandlePage } from "@/types";

function page(offset: number, total = 10_000): CandlePage {
  const count = Math.min(CANDLE_PAGE_SIZE, total - offset);
  return {
    items: Array.from({ length: count }, (_, index) => ({
      ts: new Date(Date.UTC(2020, 0, 1, offset + index)).toISOString(),
      open: offset + index,
      high: offset + index + 1,
      low: offset + index - 1,
      close: offset + index,
      volume: 0,
    })),
    offset,
    limit: CANDLE_PAGE_SIZE,
    total,
    has_previous: offset > 0,
    has_next: offset + count < total,
    min_ts: null,
    max_ts: null,
  };
}

describe("bounded absolute replay", () => {
  beforeEach(() => {
    useActiveTradeStore.getState().reset();
    useReplayStore.getState().reset();
  });

  it("requests the aligned page when scrubbing outside the loaded window", () => {
    useReplayStore.getState().setPage(page(0));
    useReplayStore.getState().scrub(3_123);
    const state = useReplayStore.getState();
    expect(state.cursor).toBe(3_123);
    expect(state.requestedOffset).toBe(CANDLE_PAGE_SIZE);
  });

  it("keeps absolute ordinals while bounding memory and chart data", () => {
    useReplayStore.getState().setPage(page(0));
    useReplayStore.getState().setPage(page(CANDLE_PAGE_SIZE));
    useReplayStore.getState().setPage(page(CANDLE_PAGE_SIZE * 2));
    useReplayStore.getState().scrub(5_500);
    const state = useReplayStore.getState();

    expect(state.candles.length).toBeLessThanOrEqual(CANDLE_PAGE_SIZE * 3);
    expect(getCandleAt(state, 5_500)?.open).toBe(5_500);
    expect(selectVisibleCandles(state).length).toBeLessThanOrEqual(1_000);
  });

  it("stores bookmarks as absolute session ordinals", () => {
    useReplayStore.getState().setPage(page(CANDLE_PAGE_SIZE));
    useReplayStore.getState().scrub(2_500);
    useReplayStore.getState().markSignal();
    expect(useReplayStore.getState().signalBookmarkIdx).toBe(2_500);
  });
});
