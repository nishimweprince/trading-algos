import { Button } from "@/components/ui/button";
import { AUTO_FILL_MIN_CONFIDENCE } from "@/lib/barTags";
import { cn } from "@/lib/utils";
import type { BarTag } from "@/types";

interface BarTagsChipsProps {
  tags: BarTag[];
  /** The setup currently in the form, so the matching chip reads as chosen. */
  selected: string | null;
  /** Setup id to display name; the parent already holds the setup list. */
  label: (setupId: string) => string;
  onSelect: (tag: BarTag) => void;
  minConfidence?: number;
  className?: string;
}

/**
 * The patterns the server found on the bar at the cursor.
 *
 * Read-only output that happens to be actionable: clicking a chip is the
 * operator choosing that setup, which is why these are buttons rather than
 * badges. `operator` marks the chosen one because that colour is reserved for
 * operator intent.
 */
export function BarTagsChips({
  tags,
  selected,
  label,
  onSelect,
  minConfidence = AUTO_FILL_MIN_CONFIDENCE,
  className,
}: BarTagsChipsProps) {
  if (tags.length === 0) return null;

  return (
    <div className={cn("flex flex-wrap gap-1", className)}>
      {tags.map((tag) => {
        const on = tag.setup_id === selected;
        // The number earns its space only when it explains something: a tag that
        // cleared the bar filled the field, and needs no defence.
        const marginal = tag.confidence < minConfidence;
        return (
          <Button
            key={`${tag.setup_id}:${tag.state}`}
            type="button"
            variant={on ? "operator" : "outline"}
            size="chip"
            aria-pressed={on}
            title={`${tag.source} · match quality ${tag.confidence.toFixed(2)}`}
            onClick={() => onSelect(tag)}
          >
            {label(tag.setup_id)}
            {tag.state !== "complete" && (
              <span className="text-white/40">{tag.state}</span>
            )}
            {marginal && <span className="tnum text-white/40">{tag.confidence.toFixed(2)}</span>}
          </Button>
        );
      })}
    </div>
  );
}
