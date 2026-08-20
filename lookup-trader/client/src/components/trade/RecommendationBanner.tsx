import { ArrowDown, ArrowUp, Minus } from "lucide-react";
import { cn } from "@/lib/utils";
import type { RecommendationPayload, RecommendationVerdict } from "@/types";

interface RecommendationBannerProps {
  recommendation: RecommendationPayload;
  /** Direction the underlying stats were scored for (long = 1, short = -1). */
  side: 1 | -1;
  scoredDirection?: "long" | "short" | null;
  targetAtr?: number;
  stopAtr?: number;
  horizon?: number;
  className?: string;
}

function DirectionArrow({
  direction,
  className,
}: {
  direction: "up" | "down" | "flat";
  className?: string;
}) {
  const iconClass = cn("h-5 w-5 shrink-0 stroke-[2.5]", className);
  if (direction === "up") return <ArrowUp className={iconClass} aria-hidden="true" />;
  if (direction === "down") return <ArrowDown className={iconClass} aria-hidden="true" />;
  return <Minus className={iconClass} aria-hidden="true" />;
}

function verdictAccent(verdict: RecommendationVerdict): string | undefined {
  if (verdict === "buy") return "text-green-500";
  if (verdict === "sell") return "text-red-500";
  if (verdict === "lean_long" || verdict === "lean_short") return "text-amber-500";
  return "text-[var(--color-foreground)]";
}

export function RecommendationBanner({
  recommendation,
  side,
  scoredDirection,
  targetAtr,
  stopAtr,
  horizon,
  className,
}: RecommendationBannerProps) {
  const { verdict, headline, rationale, caveats } = recommendation;
  const accent = verdictAccent(verdict);

  const hypothesisArrow = side === 1 ? "up" : "down";
  const hypothesisLabel =
    scoredDirection === "long"
      ? "Long"
      : scoredDirection === "short"
        ? "Short"
        : side === 1
          ? "Long"
          : "Short";

  const verdictArrow =
    verdict === "buy" || verdict === "lean_long"
      ? "up"
      : verdict === "sell" || verdict === "lean_short"
        ? "down"
        : "flat";

  const geometry =
    targetAtr != null && stopAtr != null && horizon != null
      ? `${targetAtr}× target · ${stopAtr}× stop · ${horizon} bars`
      : null;

  return (
    <div
      className={cn(
        "space-y-3 border border-[var(--color-border)] bg-[var(--color-background)] px-3 py-3",
        className,
      )}
      role="status"
    >
      <div className="flex items-start gap-3">
        <DirectionArrow direction={hypothesisArrow} className="text-[var(--color-foreground)] opacity-80" />
        <div className="min-w-0 space-y-0.5">
          <p className="text-xs uppercase text-[var(--color-muted-foreground)]">
            Scored direction
          </p>
          <p className="text-sm text-[var(--color-foreground)]">
            {hypothesisLabel} read
            {geometry && (
              <span className="text-[var(--color-muted-foreground)]"> · {geometry}</span>
            )}
          </p>
        </div>
      </div>

      <div className="h-px bg-[var(--color-border)]" />

      <div className="flex items-start gap-3">
        <DirectionArrow direction={verdictArrow} className={accent} />
        <div className="min-w-0 space-y-1">
          <p className={cn("text-base font-medium", accent)}>
            {headline}
          </p>
          <p className="text-sm leading-snug text-[var(--color-muted-foreground)]">{rationale}</p>
          {caveats.length > 0 && (
            <ul className="space-y-0.5 pt-1 text-xs text-[var(--color-muted-foreground)]">
              {caveats.map((c) => (
                <li key={c} className="flex gap-2">
                  <span className="opacity-60">—</span>
                  <span>{c}</span>
                </li>
              ))}
            </ul>
          )}
          <p className="pt-1 text-[10px] uppercase text-[var(--color-muted-foreground)] opacity-70">
            Past bars only — not a forecast
          </p>
        </div>
      </div>
    </div>
  );
}
