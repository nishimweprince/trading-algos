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
  return "text-white";
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
    verdict === "buy" ? "up" : verdict === "sell" ? "down" : "flat";

  const geometry =
    targetAtr != null && stopAtr != null && horizon != null
      ? `${targetAtr}× target · ${stopAtr}× stop · ${horizon} bars`
      : null;

  return (
    <div
      className={cn(
        "space-y-3 border border-white/15 bg-black px-3 py-3",
        className,
      )}
      role="status"
    >
      <div className="flex items-start gap-3">
        <DirectionArrow direction={hypothesisArrow} className="text-white/80" />
        <div className="min-w-0 space-y-0.5">
          <p className="text-xs uppercase tracking-widest text-white/45">
            Scored direction
          </p>
          <p className="text-sm text-white">
            {hypothesisLabel} read
            {geometry && (
              <span className="text-white/55"> · {geometry}</span>
            )}
          </p>
        </div>
      </div>

      <div className="h-px bg-white/10" />

      <div className="flex items-start gap-3">
        <DirectionArrow direction={verdictArrow} className={accent} />
        <div className="min-w-0 space-y-1">
          <p className={cn("text-base font-medium tracking-tight", accent)}>
            {headline}
          </p>
          <p className="text-sm leading-snug text-white/70">{rationale}</p>
          {caveats.length > 0 && (
            <ul className="space-y-0.5 pt-1 text-xs text-white/50">
              {caveats.map((c) => (
                <li key={c} className="flex gap-2">
                  <span className="text-white/30">—</span>
                  <span>{c}</span>
                </li>
              ))}
            </ul>
          )}
          <p className="pt-1 text-[10px] uppercase tracking-widest text-white/35">
            Past bars only — not a forecast
          </p>
        </div>
      </div>
    </div>
  );
}
