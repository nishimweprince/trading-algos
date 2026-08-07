import { create } from "zustand";
import { useActiveTradeStore } from "@/stores/activeTradeStore";
import { useSignalStore } from "@/stores/signalStore";
import type { Candle, CandlePage } from "@/types";

export type ReplaySpeed = 1 | 2 | 4;
export const CANDLE_PAGE_SIZE = 2048;
export const MAX_RENDERED_CANDLES = 1000;

interface ReplayState {
  /** Bounded contiguous window, never the full session. */
  candles: Candle[];
  loadedStartOrdinal: number;
  sessionTotal: number;
  /** Absolute ordinal within the selected session range. */
  cursor: number;
  requestedOffset: number;
  isPlaying: boolean;
  speed: ReplaySpeed;
  maxCursorSeen: number;
  baseRateSeen: boolean;
  barEnteredAt: number;
  signalBookmarkIdx: number | null;
  signalBookmarkTs: string | null;
  setPage: (page: CandlePage) => void;
  requestOrdinal: (ordinal: number) => void;
  markBaseRateSeen: () => void;
  markSignal: () => void;
  clearSignal: () => void;
  play: () => void;
  pause: () => void;
  toggle: () => void;
  advance: () => void;
  step: (delta: number) => void;
  scrub: (index: number) => void;
  setSpeed: (speed: ReplaySpeed) => void;
  reset: () => void;
}

function minCursor(): number {
  return useActiveTradeStore.getState().getMinCursor();
}

function clampAbsolute(state: ReplayState, ordinal: number): number {
  if (state.sessionTotal <= 0) return 0;
  return Math.max(minCursor(), Math.min(state.sessionTotal - 1, ordinal));
}

function pageOffset(ordinal: number): number {
  return Math.floor(Math.max(0, ordinal) / CANDLE_PAGE_SIZE) * CANDLE_PAGE_SIZE;
}

function inLoadedWindow(state: ReplayState, ordinal: number): boolean {
  return ordinal >= state.loadedStartOrdinal && ordinal < state.loadedStartOrdinal + state.candles.length;
}

function moveTo(state: ReplayState, cursor: number) {
  return {
    cursor,
    requestedOffset: inLoadedWindow(state, cursor) ? state.requestedOffset : pageOffset(cursor),
    maxCursorSeen: Math.max(state.maxCursorSeen, cursor),
    barEnteredAt: cursor === state.cursor ? state.barEnteredAt : Date.now(),
  };
}

export function getCandleAt(state: Pick<ReplayState, "candles" | "loadedStartOrdinal">, ordinal: number) {
  return state.candles[ordinal - state.loadedStartOrdinal] ?? null;
}

export const useReplayStore = create<ReplayState>((set, get) => ({
  candles: [],
  loadedStartOrdinal: 0,
  sessionTotal: 0,
  cursor: 0,
  requestedOffset: 0,
  isPlaying: false,
  speed: 1,
  maxCursorSeen: 0,
  baseRateSeen: false,
  barEnteredAt: Date.now(),
  signalBookmarkIdx: null,
  signalBookmarkTs: null,
  setPage: (page) => {
    const state = get();
    const existing = new Map<number, Candle>();
    state.candles.forEach((candle, index) => existing.set(state.loadedStartOrdinal + index, candle));
    page.items.forEach((candle, index) => existing.set(page.offset + index, candle));

    const center = page.offset;
    const keepStart = Math.max(0, center - CANDLE_PAGE_SIZE);
    const keepEnd = Math.min(page.total, center + CANDLE_PAGE_SIZE * 2);
    const ordinals = [...existing.keys()]
      .filter((ordinal) => ordinal >= keepStart && ordinal < keepEnd)
      .sort((a, b) => a - b);
    const start = ordinals[0] ?? page.offset;
    const end = ordinals[ordinals.length - 1] ?? page.offset - 1;
    const candles: Candle[] = [];
    for (let ordinal = start; ordinal <= end; ordinal++) {
      const candle = existing.get(ordinal);
      if (!candle) break;
      candles.push(candle);
    }
    const firstLoad = state.sessionTotal === 0;
    set({
      candles,
      loadedStartOrdinal: start,
      sessionTotal: page.total,
      cursor: firstLoad ? page.offset : Math.min(state.cursor, Math.max(0, page.total - 1)),
      requestedOffset: page.offset,
      isPlaying: false,
      maxCursorSeen: firstLoad ? page.offset : state.maxCursorSeen,
      barEnteredAt: Date.now(),
    });
  },
  requestOrdinal: (ordinal) => {
    const state = get();
    const cursor = clampAbsolute(state, ordinal);
    set({ ...moveTo(state, cursor), isPlaying: false });
  },
  markBaseRateSeen: () => {
    if (!get().baseRateSeen) set({ baseRateSeen: true });
  },
  markSignal: () => {
    const state = get();
    const bar = getCandleAt(state, state.cursor);
    if (!bar) return;
    useSignalStore.getState().reset();
    set({ signalBookmarkIdx: state.cursor, signalBookmarkTs: bar.ts });
  },
  clearSignal: () => {
    useSignalStore.getState().reset();
    set({ signalBookmarkIdx: null, signalBookmarkTs: null });
  },
  play: () => {
    const state = get();
    if (!state.sessionTotal || state.cursor >= state.sessionTotal - 1) return;
    set({ isPlaying: true });
  },
  pause: () => set({ isPlaying: false }),
  toggle: () => (get().isPlaying ? get().pause() : get().play()),
  advance: () => {
    const state = get();
    if (state.cursor >= state.sessionTotal - 1) {
      set({ isPlaying: false });
      return;
    }
    const target = state.cursor + 1;
    if (!inLoadedWindow(state, target)) {
      set({ ...moveTo(state, target), isPlaying: false });
      return;
    }
    set(moveTo(state, target));
  },
  step: (delta) => {
    const state = get();
    set({ ...moveTo(state, clampAbsolute(state, state.cursor + delta)), isPlaying: false });
  },
  scrub: (index) => {
    const state = get();
    set({ ...moveTo(state, clampAbsolute(state, index)), isPlaying: false });
  },
  setSpeed: (speed) => set({ speed }),
  reset: () => {
    useSignalStore.getState().reset();
    set({
      candles: [],
      loadedStartOrdinal: 0,
      sessionTotal: 0,
      cursor: 0,
      requestedOffset: 0,
      isPlaying: false,
      speed: 1,
      maxCursorSeen: 0,
      baseRateSeen: false,
      barEnteredAt: Date.now(),
      signalBookmarkIdx: null,
      signalBookmarkTs: null,
    });
  },
}));

export function captureProvenance(signalIdx: number) {
  const { maxCursorSeen, barEnteredAt, baseRateSeen } = useReplayStore.getState();
  return {
    peeked: maxCursorSeen > signalIdx,
    max_cursor_before_arm: maxCursorSeen,
    decision_ms: Math.max(0, Date.now() - barEnteredAt),
    level_revisions: useActiveTradeStore.getState().levelRevisions,
    bars_visible_at_signal: signalIdx + 1,
    saw_base_rate: baseRateSeen,
  };
}

export function useVisibleCandles() {
  const candles = useReplayStore((s) => s.candles);
  const cursor = useReplayStore((s) => s.cursor);
  const start = useReplayStore((s) => s.loadedStartOrdinal);
  return selectVisibleCandles({ candles, cursor, loadedStartOrdinal: start });
}

export function selectVisibleCandles(
  state: Pick<ReplayState, "candles" | "cursor" | "loadedStartOrdinal">,
) {
  const { candles, cursor, loadedStartOrdinal: start } = state;
  const revealedCount = Math.max(0, Math.min(candles.length, cursor - start + 1));
  return candles.slice(Math.max(0, revealedCount - MAX_RENDERED_CANDLES), revealedCount);
}

export function useCurrentBar() {
  const candles = useReplayStore((s) => s.candles);
  const cursor = useReplayStore((s) => s.cursor);
  const loadedStartOrdinal = useReplayStore((s) => s.loadedStartOrdinal);
  return getCandleAt({ candles, loadedStartOrdinal }, cursor);
}
