import { useEffect } from "react";
import { useReplayStore } from "./useReplay";

const BASE_INTERVAL_MS = 500;

export function useReplayPlayback() {
  const isPlaying = useReplayStore((s) => s.isPlaying);
  const speed = useReplayStore((s) => s.speed);
  const cursor = useReplayStore((s) => s.cursor);
  const candles = useReplayStore((s) => s.candles);
  const step = useReplayStore((s) => s.step);
  const pause = useReplayStore((s) => s.pause);

  useEffect(() => {
    if (!isPlaying || candles.length === 0) return;
    if (cursor >= candles.length - 1) {
      pause();
      return;
    }
    const id = window.setInterval(() => {
      const current = useReplayStore.getState().cursor;
      if (current >= useReplayStore.getState().candles.length - 1) {
        pause();
        return;
      }
      step(1);
    }, BASE_INTERVAL_MS / speed);
    return () => window.clearInterval(id);
  }, [isPlaying, speed, cursor, candles.length, step, pause]);
}
