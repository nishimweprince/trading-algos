import { create } from "zustand";
import { useActiveTradeStore } from "@/stores/activeTradeStore";
import { useSignalStore } from "@/stores/signalStore";
import type { Candle } from "@/types";

export type ReplaySpeed = 1 | 2 | 4;

interface ReplayState {
  candles: Candle[];
  cursor: number;
  isPlaying: boolean;
  speed: ReplaySpeed;
  /**
   * Furthest bar ever revealed this session. The chart never renders past the
   * cursor, but nothing stops the operator running forward, looking, and
   * scrubbing back before marking a setup — so the high-water mark is what makes
   * a hindsight-contaminated label distinguishable from an honest one.
   * Deliberately not reset when a trade starts.
   */
  maxCursorSeen: number;
  /** When the cursor last landed on the current bar; feeds decision latency. */
  barEnteredAt: number;
  /** Pinned setup bar for skip labelling — survives cursor movement during playback. */
  signalBookmarkIdx: number | null;
  signalBookmarkTs: string | null;
  setCandles: (candles: Candle[]) => void;
  markSignal: () => void;
  clearSignal: () => void;
  play: () => void;
  pause: () => void;
  toggle: () => void;
  /** Playback tick. Moves the cursor and leaves isPlaying alone. */
  advance: () => void;
  /** Operator-initiated move. Taking manual control also stops playback. */
  step: (delta: number) => void;
  scrub: (index: number) => void;
  setSpeed: (speed: ReplaySpeed) => void;
  reset: () => void;
}

function clampCursor(candles: Candle[], index: number, minIndex = 0): number {
  if (candles.length === 0) return 0;
  return Math.max(minIndex, Math.min(candles.length - 1, index));
}

function minCursor(): number {
  return useActiveTradeStore.getState().getMinCursor();
}

/** Cursor move bookkeeping shared by every path that changes the cursor. */
function moveTo(state: ReplayState, cursor: number) {
  return {
    cursor,
    maxCursorSeen: Math.max(state.maxCursorSeen, cursor),
    barEnteredAt: cursor === state.cursor ? state.barEnteredAt : Date.now(),
  };
}

export const useReplayStore = create<ReplayState>((set, get) => ({
  candles: [],
  cursor: 0,
  isPlaying: false,
  speed: 1,
  maxCursorSeen: 0,
  barEnteredAt: Date.now(),
  signalBookmarkIdx: null,
  signalBookmarkTs: null,
  setCandles: (candles) => {
    useSignalStore.getState().reset();
    set({
      candles,
      cursor: 0,
      isPlaying: false,
      maxCursorSeen: 0,
      barEnteredAt: Date.now(),
      signalBookmarkIdx: null,
      signalBookmarkTs: null,
    });
  },
  markSignal: () => {
    const { candles, cursor } = get();
    const bar = candles[cursor];
    if (!bar) return;
    useSignalStore.getState().setActiveSignalId(null);
    useSignalStore.getState().setLastSignal(null);
    set({ signalBookmarkIdx: cursor, signalBookmarkTs: bar.ts });
  },
  clearSignal: () => {
    useSignalStore.getState().reset();
    set({ signalBookmarkIdx: null, signalBookmarkTs: null });
  },
  play: () => {
    const { candles, cursor } = get();
    if (candles.length === 0 || cursor >= candles.length - 1) return;
    set({ isPlaying: true });
  },
  pause: () => set({ isPlaying: false }),
  toggle: () => (get().isPlaying ? get().pause() : get().play()),
  advance: () => {
    const state = get();
    if (state.cursor >= state.candles.length - 1) {
      set({ isPlaying: false });
      return;
    }
    set(moveTo(state, state.cursor + 1));
  },
  step: (delta) => {
    const state = get();
    set({
      ...moveTo(state, clampCursor(state.candles, state.cursor + delta, minCursor())),
      isPlaying: false,
    });
  },
  scrub: (index) => {
    const state = get();
    set({
      ...moveTo(state, clampCursor(state.candles, index, minCursor())),
      isPlaying: false,
    });
  },
  setSpeed: (speed) => set({ speed }),
  reset: () =>
    set({
      candles: [],
      cursor: 0,
      isPlaying: false,
      speed: 1,
      maxCursorSeen: 0,
      barEnteredAt: Date.now(),
      signalBookmarkIdx: null,
      signalBookmarkTs: null,
    }),
}));

/** Snapshot of how the operator arrived at this bar, taken when a trade is armed. */
export function captureProvenance(signalIdx: number) {
  const { maxCursorSeen, barEnteredAt } = useReplayStore.getState();
  return {
    peeked: maxCursorSeen > signalIdx,
    max_cursor_before_arm: maxCursorSeen,
    decision_ms: Math.max(0, Date.now() - barEnteredAt),
    level_revisions: useActiveTradeStore.getState().levelRevisions,
    bars_visible_at_signal: signalIdx,
  };
}

export function useVisibleCandles() {
  const candles = useReplayStore((s) => s.candles);
  const cursor = useReplayStore((s) => s.cursor);
  return candles.slice(0, cursor + 1);
}

export function useCurrentBar() {
  const candles = useReplayStore((s) => s.candles);
  const cursor = useReplayStore((s) => s.cursor);
  return candles[cursor] ?? null;
}
