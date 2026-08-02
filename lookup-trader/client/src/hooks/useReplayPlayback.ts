import { useEffect } from "react";
import { useReplayStore } from "./useReplay";

const BASE_INTERVAL_MS = 500;

/**
 * Drives the cursor while playing. The interval depends only on isPlaying and
 * speed — not on the cursor — so a tick never tears down its own timer, and the
 * period stays true to the selected speed.
 */
export function useReplayPlayback() {
  const isPlaying = useReplayStore((s) => s.isPlaying);
  const speed = useReplayStore((s) => s.speed);

  useEffect(() => {
    if (!isPlaying) return;

    const id = window.setInterval(() => {
      useReplayStore.getState().advance();
    }, BASE_INTERVAL_MS / speed);

    return () => window.clearInterval(id);
  }, [isPlaying, speed]);
}
