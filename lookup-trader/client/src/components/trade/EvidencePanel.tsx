import { ArrowDown, ArrowUp, Minus } from "lucide-react";
import { cn } from "@/lib/utils";
import type {
  BaseRate,
  BaseRateQuery,
  MetaReplayInference,
  MetaReplayPrediction,
  RecommendationVerdict,
} from "@/types";

const DIMENSION_LABELS: Record<string, string> = {
  tag_setup_id: "candlestick pattern",
  session_overlap: "session overlap",
  day_of_week: "day",
  htf_atr_bucket: "H4 volatility",
  htf_trend_state: "H4 trend",
  ema_slope_bucket: "EMA slope",
  atr_change_bucket: "volatility change",
  rsi_band: "RSI",
  atr_bucket: "volatility",
  session: "session",
  trend_state: "trend",
};

function percent(value: number): string {
  if (value > 0 && value < 0.001) return "<0.1%";
  return `${(value * 100).toFixed(1)}%`;
}

function rValue(value: number | null | undefined, signed = false): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const prefix = signed && value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(2)}R`;
}

function verdictPresentation(verdict: RecommendationVerdict) {
  if (verdict === "buy" || verdict === "lean_long") {
    return { icon: ArrowUp, tone: "text-emerald-400" };
  }
  if (verdict === "sell" || verdict === "lean_short") {
    return { icon: ArrowDown, tone: "text-rose-400" };
  }
  if (verdict === "wait") return { icon: Minus, tone: "text-amber-300" };
  return { icon: Minus, tone: "text-white/65" };
}

function ContextScope({ result }: { result: BaseRate }) {
  if (!result.fallback_used) {
    return <span>Exact context</span>;
  }
  const dropped = result.dropped_dimensions
    .map((dimension) => DIMENSION_LABELS[dimension] ?? dimension.replaceAll("_", " "))
    .join(" + ");
  return (
    <span>
      Broader context<span className="text-white/25"> · </span>{dropped || "narrow filters"} dropped
    </span>
  );
}

function ActiveModelDecision({
  result,
  side,
}: {
  result: MetaReplayPrediction;
  side: 1 | -1;
}) {
  const probabilityPosition = `${Math.min(Math.max(result.probability * 100, 0), 100)}%`;
  const thresholdPosition = `${Math.min(Math.max(result.threshold * 100, 0), 100)}%`;
  const direction = side === 1 ? "Long" : "Short";
  const DirectionIcon = side === 1 ? ArrowUp : ArrowDown;
  const directionTone = side === 1 ? "text-emerald-400" : "text-rose-400";
  const decisionTone = result.would_take
    ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-300"
    : "border-amber-300/30 bg-amber-300/10 text-amber-200";
  const probabilityTone = result.would_take ? "bg-emerald-400" : "bg-amber-300";
  const label = `${direction} recommendation: ${percent(result.probability)} positive net outcome probability, ${percent(result.threshold)} take threshold, would take ${result.would_take ? "yes" : "no"}`;

  return (
    <div className="mt-3 min-w-0 space-y-4" aria-label={label}>
      <div className="grid grid-cols-2 gap-2">
        <div className="border border-white/10 bg-black/15 px-3 py-2.5">
          <p className="text-[9px] text-white/40 uppercase">
            Recommended direction
          </p>
          <div className={cn("mt-1.5 flex items-center gap-1.5", directionTone)}>
            <DirectionIcon className="h-5 w-5 stroke-[2.5]" />
            <span className="text-sm font-semibold uppercase">{direction}</span>
          </div>
        </div>
        <div className={cn("border px-3 py-2.5", decisionTone)}>
          <p className="text-[9px] opacity-65 uppercase">Would take</p>
          <p className="mt-1.5 text-sm font-semibold uppercase">
            {result.would_take ? "Yes" : "No — skip"}
          </p>
        </div>
      </div>

      <div className="space-y-2 tabular-nums">
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-xs text-white/55">Positive net outcome probability</span>
          <span className={cn("text-sm font-semibold", result.would_take ? "text-emerald-300" : "text-amber-200")}>
            {percent(result.probability)}
          </span>
        </div>
        <div className="relative h-2 overflow-visible bg-white/10" aria-hidden="true">
          <div
            className={cn("absolute inset-y-0 left-0 opacity-75", probabilityTone)}
            style={{ width: probabilityPosition }}
          />
          <div className="absolute -top-1 h-4 w-px bg-white" style={{ left: thresholdPosition }} />
        </div>
        <div className="flex justify-between gap-3 text-[10px] leading-none text-white/40">
          <span>0%</span>
          <span>take threshold {percent(result.threshold)}</span>
          <span>100%</span>
        </div>
      </div>
    </div>
  );
}

export function EvidencePanel({
  query,
  result,
  modelShadow,
  modelShadowError,
  modelShadowLoading,
}: {
  query: BaseRateQuery;
  result: BaseRate;
  modelShadow: MetaReplayInference | null;
  modelShadowError: Error | null;
  modelShadowLoading: boolean;
}) {
  const recommendation = result.recommendation;
  const verdict = recommendation?.verdict ?? "insufficient_data";
  const presentation = verdictPresentation(verdict);
  const VerdictIcon = presentation.icon;
  const timeframe = modelShadow?.timeframe ?? query.timeframe;
  const horizon = modelShadow?.contract.horizon_bars ?? query.horizon ?? 24;
  const target = modelShadow?.contract.target_atr ?? query.targetAtr ?? 3;
  const stop = modelShadow?.contract.stop_atr ?? query.stopAtr ?? 2;
  const activePrediction =
    modelShadow?.predictions.find((prediction) => prediction.role === "active") ?? null;
  const point = result.expectancy_r_net ?? result.expectancy_r;
  const evidenceBars =
    result.level_used === "no_signal"
      ? (result.decided_available ?? result.resolved_count)
      : result.resolved_count;
  const evidenceWeeks =
    result.level_used === "no_signal"
      ? (result.periods_available ?? result.independent_periods ?? 0)
      : (result.independent_periods ?? 0);

  return (
    <section
      className="border border-[#2A2A2E] bg-[#151517] px-3 py-3 text-[#FAFAFA]"
      aria-label="Trading evidence"
    >
      <header className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 border-b border-[#2A2A2E] pb-2">
        <p className="text-[10px] font-medium text-white/55 uppercase">
          Evidence · {timeframe} · {horizon} bars · {target} ATR target / {stop} ATR stop
        </p>
        <span className="text-[10px] text-white/35 uppercase">
          empirical history
        </span>
      </header>

      <div className="py-3" role="status">
        <div className="flex items-start gap-1">
          <VerdictIcon className={cn("mt-0.5 h-4 w-4 shrink-0 stroke-[2.4]", presentation.tone)} />
          <div className="min-w-0 flex-1">
            <p className={cn("text-sm font-medium", presentation.tone)}>
              {recommendation?.headline ?? "Insufficient data"}
            </p>
            <p className="mt-0.5 max-w-2xl text-xs leading-snug text-white/65">
              {recommendation?.rationale ?? "Not enough resolved history to trust this yet."}
            </p>
            {point != null && (
              <p className="mt-2 text-xs tabular-nums text-white/80">
                {rValue(point, true)} estimated net
                {result.net_expectancy_ci_low_r != null &&
                  result.net_expectancy_ci_high_r != null && (
                    <span className="text-white/45">
                      {" "}· 95% range {rValue(result.net_expectancy_ci_low_r, true)} to{" "}
                      {rValue(result.net_expectancy_ci_high_r, true)}
                    </span>
                  )}
              </p>
            )}
            <p className="mt-1 text-xs text-white/45">
              {result.win_rate != null && `${percent(result.win_rate)} won · `}
              {evidenceBars} resolved bars · {evidenceWeeks} weeks
              {result.level_used === "no_signal" && (
                <span>
                  {" "}· need {result.min_samples_required ?? "—"} bars /{" "}
                  {result.min_periods_required ?? "—"} weeks
                </span>
              )}
            </p>
            <p className="mt-1 text-xs text-white/45">
              <ContextScope result={result} />
            </p>
          </div>
        </div>
      </div>

      <div className="border-t border-[#2A2A2E] pt-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <p className="text-[10px] font-medium text-white/55 uppercase">
            Model recommendation
          </p>
          <span className="text-[10px] text-[#C8A96A] uppercase">
            informational only · unpromoted
          </span>
        </div>

        {modelShadowLoading && (
          <p className="mt-2 text-xs text-white/40">Loading causal replay inference…</p>
        )}
        {modelShadowError && (
          <p className="mt-2 text-xs text-white/40">Unavailable — {modelShadowError.message}</p>
        )}
        {modelShadow && activePrediction && (
          <>
            <ActiveModelDecision result={activePrediction} side={modelShadow.side} />
            <details className="mt-3 border-t border-white/10 pt-2 text-[10px] text-white/35">
              <summary className="w-fit cursor-pointer rounded-sm uppercase outline-none focus-visible:ring-1 focus-visible:ring-[#C8A96A]">
                Artifact and version details
              </summary>
              <dl className="mt-2 grid gap-x-4 gap-y-1 sm:grid-cols-[auto_1fr]">
                <dt>Active artifact</dt>
                <dd className="break-all text-white/55">{activePrediction.artifact_version}</dd>
                <dt>Contract</dt>
                <dd className="text-white/55">
                  next H1 open · {modelShadow.contract.horizon_bars} observed bars · {modelShadow.contract.target_atr} ATR
                  target · {modelShadow.contract.stop_atr} ATR stop
                </dd>
                <dt>Side</dt>
                <dd className="text-white/55">
                  {modelShadow.side === 1 ? "long" : "short"} · calendar coverage{" "}
                  {modelShadow.calendar_coverage_ok ? "trusted" : "unavailable"}
                </dd>
                <dt>Method</dt>
                <dd className="text-white/55">direction from context · take/skip from active model</dd>
              </dl>
            </details>
          </>
        )}
      </div>

      <p className="mt-3 border-t border-white/10 pt-2 text-[10px] text-white/25 uppercase">
        Past bars only · not a forecast
      </p>
    </section>
  );
}
