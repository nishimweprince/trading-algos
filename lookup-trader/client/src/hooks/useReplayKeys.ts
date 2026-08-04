import { useEffect } from "react";
import { useReplayStore } from "./useReplay";
import { useActiveTradeStore } from "@/stores/activeTradeStore";

const JUMP_BARS = 10;

/** True while the operator is typing, so shortcuts never eat trade-form input. */
function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

/**
 * Keyboard transport for the replay cursor. Bar-by-bar labelling is a
 * hundreds-of-presses task; the mouse should be optional.
 *
 *   Space          play / pause
 *   S              mark signal bar (for skip labelling)
 *   ← →            step one bar
 *   Shift + ← →    jump ten bars
 *   Home / End     first / last bar
 */
export function useReplayKeys(enabled = true) {
  useEffect(() => {
    if (!enabled) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (isTyping(event.target)) return;

      const store = useReplayStore.getState();
      if (store.candles.length === 0) return;

      switch (event.key) {
        case " ":
        case "Spacebar":
          event.preventDefault(); // otherwise the page scrolls
          store.toggle();
          break;
        case "s":
        case "S":
          event.preventDefault();
          store.markSignal();
          break;
        case "ArrowRight":
          event.preventDefault();
          store.step(event.shiftKey ? JUMP_BARS : 1);
          break;
        case "ArrowLeft":
          event.preventDefault();
          store.step(event.shiftKey ? -JUMP_BARS : -1);
          break;
        case "Home":
          event.preventDefault();
          store.scrub(useActiveTradeStore.getState().getMinCursor());
          break;
        case "End":
          event.preventDefault();
          store.scrub(store.candles.length - 1);
          break;
        default:
          break;
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled]);
}
