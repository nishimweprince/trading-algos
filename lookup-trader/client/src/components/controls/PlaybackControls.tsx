import { Pause, Play, SkipBack, SkipForward } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Slider } from "@/components/ui/slider";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useReplayStore, type ReplaySpeed } from "@/hooks/useReplay";

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

  if (candles.length === 0) return null;

  return (
    <div className="flex flex-col gap-3 border-t border-zinc-800 bg-zinc-950 p-4">
      <div className="flex items-center gap-2">
        <Button variant="outline" size="icon" onClick={() => step(-1)} disabled={cursor <= 0}>
          <SkipBack className="h-4 w-4" />
        </Button>
        {isPlaying ? (
          <Button variant="default" size="icon" onClick={pause}>
            <Pause className="h-4 w-4" />
          </Button>
        ) : (
          <Button variant="default" size="icon" onClick={play} disabled={cursor >= candles.length - 1}>
            <Play className="h-4 w-4" />
          </Button>
        )}
        <Button variant="outline" size="icon" onClick={() => step(1)} disabled={cursor >= candles.length - 1}>
          <SkipForward className="h-4 w-4" />
        </Button>
        <div className="ml-4 flex items-center gap-2 text-xs text-zinc-400">
          <span>Speed</span>
          <Select value={String(speed)} onValueChange={(v) => setSpeed(Number(v) as ReplaySpeed)}>
            <SelectTrigger className="w-20 h-8">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="1">1x</SelectItem>
              <SelectItem value="2">2x</SelectItem>
              <SelectItem value="4">4x</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="ml-auto font-mono text-xs text-zinc-400">
          Bar {cursor + 1} / {candles.length}
        </div>
      </div>
      <Slider
        value={[cursor]}
        min={0}
        max={Math.max(0, candles.length - 1)}
        step={1}
        onValueChange={([v]) => scrub(v)}
      />
    </div>
  );
}
