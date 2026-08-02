import { create } from "zustand";
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
  step: (delta: number) => void;
  scrub: (index: number) => void;
  setSpeed: (speed: ReplaySpeed) => void;
  reset: () => void;
}

export const useReplayStore = create<ReplayState>((set, get) => ({
  candles: [],
  cursor: 0,
  isPlaying: false,
  speed: 1,
  setCandles: (candles) => set({ candles, cursor: 0, isPlaying: false }),
  play: () => set({ isPlaying: true }),
  pause: () => set({ isPlaying: false }),
  step: (delta) => {
    const { candles, cursor } = get();
    const next = Math.max(0, Math.min(candles.length - 1, cursor + delta));
    set({ cursor: next, isPlaying: false });
  },
  scrub: (index) => {
    const { candles } = get();
    const next = Math.max(0, Math.min(candles.length - 1, index));
    set({ cursor: next, isPlaying: false });
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
