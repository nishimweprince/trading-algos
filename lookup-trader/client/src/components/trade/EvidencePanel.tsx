import { ArrowDown, ArrowUp, Minus } from "lucide-react";
import { cn } from "@/lib/utils";
import type {
  BaseRate,
  BaseRateQuery,
  OutcomeDirection,
  OutcomeShadow,
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
  if (verdict === "buy") return { icon: ArrowUp, tone: "text-green-500" };
  if (verdict === "sell") return { icon: ArrowDown, tone: "text-red-500" };
  if (verdict === "lean_long") return { icon: ArrowUp, tone: "text-[#C8A96A]" };
  if (verdict === "lean_short") return { icon: ArrowDown, tone: "text-[#C8A96A]" };
  return { icon: Minus, tone: "text-[#FAFAFA]" };
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

function ProbabilityRail({ result }: { result: OutcomeDirection }) {
  const winPosition = `${Math.min(Math.max(result.p_win * 100, 0), 100)}%`;
  const breakEvenPosition = `${Math.min(
    Math.max(result.spread_adjusted_break_even_p_win * 100, 0),
    100,
  )}%`;
  const label = `${result.direction} model shadow: ${percent(result.p_win)} win probability, ${percent(result.spread_adjusted_break_even_p_win)} spread-adjusted break-even, ${rValue(result.expected_net_r, true)} expected net`;

  return (
    <div className="min-w-0 space-y-2" aria-label={label}>
      <div className="flex items-baseline justify-between gap-3 font-mono text-xs tabular-nums">
        <span className={`text-white/85 capitalize`}>
          {result.direction} <span className="text-white">{percent(result.p_win)}</span> <span className={result.direction === "long" ? "text-green-700" : "text-red-700"}>{result.direction === "long" ? "↑" : "↓"}</span>
        </span>
        <span className={cn("whitespace-nowrap", result.expected_net_r > 0 ? "text-white" : "text-white/50")}>
          {rValue(result.expected_net_r, true)} net
        </span>
      </div>
      <div className="relative h-3" aria-hidden="true">
        <div className="absolute top-[5px] right-0 left-0 h-px bg-white/15" />
        <div
          className="absolute top-0 h-3 w-px bg-[#C8A96A]"
          style={{ left: breakEvenPosition }}
        />
        <div
          className="absolute top-[2px] h-[7px] w-[7px] -translate-x-1/2 rounded-full border border-[#151517] bg-[#FAFAFA]"
          style={{ left: winPosition }}
        />
      </div>
      <div className="flex justify-between gap-3 text-[10px] leading-none text-white/40">
        <span>
          L {percent(result.p_loss)} · T {percent(result.p_timeout)}
        </span>
        <span>break-even {percent(result.spread_adjusted_break_even_p_win)}</span>
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
  modelShadow: OutcomeShadow | null;
  modelShadowError: Error | null;
  modelShadowLoading: boolean;
}) {
  const recommendation = result.recommendation;
  const verdict = recommendation?.verdict ?? "insufficient_data";
  const presentation = verdictPresentation(verdict);
  const VerdictIcon = presentation.icon;
  const timeframe = modelShadow?.contract.timeframe ?? query.timeframe;
  const horizon = modelShadow?.contract.horizon_bars ?? query.horizon ?? 24;
  const target = modelShadow?.contract.target_atr ?? query.targetAtr ?? 1.5;
  const stop = modelShadow?.contract.stop_atr ?? query.stopAtr ?? 1;
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
          Evidence · {timeframe} · {horizon} bars · {target}R / {stop}R
        </p>
        <span className="text-[10px] text-white/35 uppercase">
          empirical history
        </span>
      </header>

      <div className="py-3" role="status">
        <div className="flex items-start gap-3">
          <VerdictIcon className={cn("mt-0.5 h-5 w-5 shrink-0 stroke-[2.4]", presentation.tone)} />
          <div className="min-w-0 flex-1">
            <p className={cn("text-lg font-medium", presentation.tone)}>
              {recommendation?.headline ?? "Insufficient data"}
            </p>
            <p className="mt-0.5 max-w-2xl text-sm leading-snug text-white/65">
              {recommendation?.rationale ?? "Not enough resolved history to trust this yet."}
            </p>
            {point != null && (
              <p className="mt-2 font-mono text-xs tabular-nums text-white/80">
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
            Model shadow
          </p>
          <span className="text-[10px] text-[#C8A96A] uppercase">
            informational only · unpromoted
          </span>
        </div>

        {modelShadowLoading && (
          <p className="mt-2 text-xs text-white/40">Loading closed-bar inference…</p>
        )}
        {modelShadowError && (
          <p className="mt-2 text-xs text-white/40">Unavailable — {modelShadowError.message}</p>
        )}
        {modelShadow && (
          <>
            <div className="mt-3 grid gap-4 sm:grid-cols-1 sm:gap-6">
              <ProbabilityRail result={modelShadow.long} />
              <ProbabilityRail result={modelShadow.short} />
            </div>
            <details className="mt-3 border-t border-white/10 pt-2 text-[10px] text-white/35">
              <summary className="w-fit cursor-pointer rounded-sm uppercase outline-none focus-visible:ring-1 focus-visible:ring-[#C8A96A]">
                Artifact and version details
              </summary>
              <dl className="mt-2 grid gap-x-4 gap-y-1 sm:grid-cols-[auto_1fr]">
                <dt>Artifact</dt>
                <dd className="break-all text-white/55">{modelShadow.artifact_version}</dd>
                <dt>Contract</dt>
                <dd className="text-white/55">
                  {modelShadow.contract.horizon_bars} H1 bars · {modelShadow.contract.target_atr} ATR
                  target · {modelShadow.contract.stop_atr} ATR stop · {modelShadow.contract.spread_pips_assumed} pips
                </dd>
                <dt>Schema</dt>
                <dd className="break-all text-white/55">{modelShadow.schema_sha256}</dd>
                <dt>Versions</dt>
                <dd className="text-white/55">
                  model {modelShadow.model_version} · features {modelShadow.feature_version} · bars{" "}
                  {modelShadow.bar_feature_version}
                </dd>
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
