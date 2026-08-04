import { cn } from "@/lib/utils";
import type { RecommendationResult } from "@/lib/recommendation";

const VERDICT_STYLES: Record<RecommendationResult["verdict"], string> = {
  buy: "border-emerald-800/50 bg-emerald-950/30 text-emerald-300",
  sell: "border-rose-800/50 bg-rose-950/30 text-rose-300",
  wait: "border-amber-800/50 bg-amber-950/30 text-amber-200",
  insufficient_data: "border-zinc-700 bg-zinc-900/50 text-zinc-400",
};

interface RecommendationBannerProps {
  recommendation: RecommendationResult;
  className?: string;
}

export function RecommendationBanner({ recommendation, className }: RecommendationBannerProps) {
  const { verdict, headline, rationale, caveats } = recommendation;

  return (
    <div
      className={cn(
        "rounded-md border px-3 py-2",
        VERDICT_STYLES[verdict],
        className,
      )}
      role="status"
    >
      <p className="text-sm font-medium">{headline}</p>
      <p className="mt-0.5 text-xs opacity-90">{rationale}</p>
      {caveats.length > 0 && (
        <ul className="mt-1.5 list-inside list-disc text-[11px] opacity-75">
          {caveats.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      )}
      <p className="mt-1.5 text-[10px] uppercase tracking-wide opacity-50">
        Based on past bars — not a live forecast
      </p>
    </div>
  );
}
