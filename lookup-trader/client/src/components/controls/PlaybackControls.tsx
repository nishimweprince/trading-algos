import { ChevronFirst, ChevronLast, Pause, Play, SkipBack, SkipForward } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Slider } from "@/components/ui/slider";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useReplayStore, type ReplaySpeed } from "@/hooks/useReplay";
import { useActiveTradeStore } from "@/stores/activeTradeStore";
import { cn } from "@/lib/utils";

const JUMP_BARS = 10;

const SHORTCUTS = [
  { keys: "Space", action: "play / pause" },
  { keys: "← →", action: "step" },
  { keys: "⇧ ← →", action: "jump 10" },
  { keys: "Home / End", action: "first / last" },
];

export function PlaybackControls() {
  const candles = useReplayStore((s) => s.candles);
  const cursor = useReplayStore((s) => s.cursor);
  const isPlaying = useReplayStore((s) => s.isPlaying);
  const speed = useReplayStore((s) => s.speed);
  const play = useReplayStore((s) => s.play);
  const pause = useReplayStore((s) => s.pause);
  const step = useReplayStore((s) => s.step);
  const scrub = useReplayStore((s) => s.scrub);
  const setSpeed = useReplayStore((s) => s.setSpeed);

  const total = candles.length;
  const loaded = total > 0;
  const minCursor = useActiveTradeStore((s) => s.getMinCursor());
  const atStart = cursor <= minCursor;
  const atEnd = loaded && cursor >= total - 1;

  return (
    <div className="flex flex-col gap-2 border-b border-zinc-800 bg-zinc-950 px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="flex items-center gap-1">
          <Button
            variant="outline"
            size="icon-sm"
            onClick={() => scrub(minCursor)}
            disabled={!loaded || atStart}
            title="First bar (Home)"
            aria-label="Go to first bar"
          >
            <ChevronFirst className="h-4 w-4" />
          </Button>
          <Button
            variant="outline"
            size="icon-sm"
            onClick={() => step(-JUMP_BARS)}
            disabled={!loaded || atStart}
            title="Back 10 bars (Shift + ←)"
            aria-label="Back ten bars"
          >
            <span className="tnum text-[10px] font-medium">-10</span>
          </Button>
          <Button
            variant="outline"
            size="icon-sm"
            onClick={() => step(-1)}
            disabled={!loaded || atStart}
            title="Back one bar (←)"
            aria-label="Back one bar"
          >
            <SkipBack className="h-4 w-4" />
          </Button>

          <Button
            variant={isPlaying ? "operator" : "default"}
            size="icon"
            className="mx-1"
            onClick={isPlaying ? pause : play}
            disabled={!loaded || (!isPlaying && atEnd)}
            title={isPlaying ? "Pause (Space)" : "Play (Space)"}
            aria-label={isPlaying ? "Pause replay" : "Play replay"}
          >
            {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
          </Button>

          <Button
            variant="outline"
            size="icon-sm"
            onClick={() => step(1)}
            disabled={!loaded || atEnd}
            title="Forward one bar (→)"
            aria-label="Forward one bar"
          >
            <SkipForward className="h-4 w-4" />
          </Button>
          <Button
            variant="outline"
            size="icon-sm"
            onClick={() => step(JUMP_BARS)}
            disabled={!loaded || atEnd}
            title="Forward 10 bars (Shift + →)"
            aria-label="Forward ten bars"
          >
            <span className="tnum text-[10px] font-medium">+10</span>
          </Button>
          <Button
            variant="outline"
            size="icon-sm"
            onClick={() => scrub(total - 1)}
            disabled={!loaded || atEnd}
            title="Last bar (End)"
            aria-label="Go to last bar"
          >
            <ChevronLast className="h-4 w-4" />
          </Button>
        </div>

        <div className="flex items-center gap-2">
          <span className="text-xs text-zinc-400">Speed</span>
          <Select
            value={String(speed)}
            onValueChange={(v) => setSpeed(Number(v) as ReplaySpeed)}
            disabled={!loaded}
          >
            <SelectTrigger className="tnum h-8 w-[4.5rem]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="1">1×</SelectItem>
              <SelectItem value="2">2×</SelectItem>
              <SelectItem value="4">4×</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="ml-auto flex items-baseline gap-2">
          <span className="text-xs text-zinc-400">Bar</span>
          <span
            className={cn(
              "tnum font-mono text-lg font-medium",
              isPlaying ? "text-operator" : "text-zinc-100",
              !loaded && "text-zinc-600",
            )}
          >
            {loaded ? cursor + 1 : "—"}
          </span>
          <span className="tnum font-mono text-xs text-zinc-500">/ {loaded ? total : "—"}</span>
        </div>
      </div>

      <Slider
        value={[loaded ? cursor : 0]}
        min={minCursor}
        max={Math.max(minCursor, total - 1)}
        step={1}
        disabled={!loaded}
        aria-label="Replay position"
        onValueChange={([v]) => scrub(v)}
        className={cn(isPlaying && "[&_[data-slot=slider-range]]:bg-operator")}
      />

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-zinc-600">
        {SHORTCUTS.map(({ keys, action }) => (
          <span key={keys} className="flex items-center gap-1.5">
            <kbd className="rounded border border-zinc-800 bg-zinc-900 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400">
              {keys}
            </kbd>
            {action}
          </span>
        ))}
      </div>
    </div>
  );
}
