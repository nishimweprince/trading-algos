import { create } from "zustand";
import { useActiveTradeStore } from "@/stores/activeTradeStore";
import type { Candle } from "@/types";

export type ReplaySpeed = 1 | 2 | 4;

interface ReplayState {
  candles: Candle[];
  cursor: number;
  isPlaying: boolean;
  speed: ReplaySpeed;
  setCandles: (candles: Candle[]) => void;
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

export const useReplayStore = create<ReplayState>((set, get) => ({
  candles: [],
  cursor: 0,
  isPlaying: false,
  speed: 1,
  setCandles: (candles) => set({ candles, cursor: 0, isPlaying: false }),
  play: () => {
    const { candles, cursor } = get();
    if (candles.length === 0 || cursor >= candles.length - 1) return;
    set({ isPlaying: true });
  },
  pause: () => set({ isPlaying: false }),
  toggle: () => (get().isPlaying ? get().pause() : get().play()),
  advance: () => {
    const { candles, cursor } = get();
    if (cursor >= candles.length - 1) {
      set({ isPlaying: false });
      return;
    }
    set({ cursor: cursor + 1 });
  },
  step: (delta) => {
    const { candles, cursor } = get();
    set({ cursor: clampCursor(candles, cursor + delta, minCursor()), isPlaying: false });
  },
  scrub: (index) => {
    const { candles } = get();
    set({ cursor: clampCursor(candles, index, minCursor()), isPlaying: false });
  },
  setSpeed: (speed) => set({ speed }),
  reset: () => set({ candles: [], cursor: 0, isPlaying: false, speed: 1 }),
}));

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
