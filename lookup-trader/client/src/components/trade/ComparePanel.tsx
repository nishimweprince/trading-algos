import { useEffect, useMemo, useState } from "react";
import { useForm, useFormContext, useWatch, type UseFormReturn } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { ChevronDown, ChevronRight, Pin } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Form } from "@/components/ui/form";
import { StatCard } from "@/components/common/StatCard";
import {
  ChipToggleField,
  ComboboxField,
  InputField,
  SelectField,
  SwitchField,
} from "@/components/common/fields";
import { toOptions, type FieldOption } from "@/lib/fieldOptions";
import { useBaseRate } from "@/hooks/useBaseRate";
import { useCandleBounds, useSignalContext } from "@/hooks/useCandles";
import { useCompare } from "@/hooks/useCompare";
import { useCurrentBar, useReplayStore } from "@/hooks/useReplay";
import { toLevel, type MarkTradeForm } from "@/hooks/useMarkTradeForm";
import { useSetupOptions } from "@/hooks/useSetups";
import { formatPercent, toUtcIso } from "@/lib/format";
import { riskReward, rrBucket } from "@/lib/pips";
import {
  CONFLUENCE_LABELS,
  CONFLUENCE_TAGS,
  ENTRY_QUALITIES,
  ENTRY_QUALITY_LABELS,
  HTF_ALIGNMENTS,
  HTF_ALIGNMENT_LABELS,
  MARKET_STRUCTURES,
  MARKET_STRUCTURE_LABELS,
  OBSERVED_TRENDS,
  OBSERVED_TREND_LABELS,
  SKIP_REASON_LABELS,
} from "@/lib/tradeLabels";
import { formatTradingSession, TRADING_SESSIONS } from "@/lib/tradingSession";
import { MAX_BARS, snapToTouchLevel } from "@/lib/constants";
import { useActiveTradeStore } from "@/stores/activeTradeStore";
import { RecommendationBanner } from "@/components/trade/RecommendationBanner";
import { breakEvenFromGeometry, breakEvenFromRr, deriveRecommendation } from "@/lib/recommendation";
import { cn } from "@/lib/utils";
import type { BaseRate, BaseRateQuery, CompareContext, Session } from "@/types";

/** Radix Select has no empty-string value, so "any" needs a sentinel. */
const ANY = "__any";

/**
 * Dimension keys are form field names, so they have to be schema keys — and
 * scalar ones: confluence_tags is a string[] handled by its own chip field.
 */
type DimensionKey = Exclude<Extract<keyof CompareContext, keyof FormValues>, "confluence_tags">;

interface Dimension {
  key: DimensionKey;
  label: string;
  options: FieldOption[];
  /** Selects hold strings; the API wants the real type. */
  parse?: (raw: string) => unknown;
}

/** Computed at the signal bar — the server knows these, so they auto-fill. */
const COMPUTED: Dimension[] = [
  {
    key: "trend_state",
    label: "Trend",
    options: [
      { value: "up", label: "Up" },
      { value: "down", label: "Down" },
    ],
  },
  {
    key: "session",
    label: "Session",
    options: TRADING_SESSIONS.map((s) => ({ value: s, label: formatTradingSession(s) })),
  },
  {
    key: "atr_bucket",
    label: "Volatility",
    options: [
      { value: "low", label: "Low" },
      { value: "mid", label: "Mid" },
      { value: "high", label: "High" },
    ],
  },
  {
    key: "rsi_band",
    label: "RSI band",
    options: [
      { value: "oversold", label: "Oversold" },
      { value: "neutral", label: "Neutral" },
      { value: "overbought", label: "Overbought" },
    ],
  },
  {
    key: "side",
    label: "Side",
    options: [
      { value: "1", label: "Long" },
      { value: "-1", label: "Short" },
    ],
    parse: Number,
  },
  {
    key: "rr_bucket",
    label: "Planned R:R",
    options: [
      { value: "low", label: "Under 1.5" },
      { value: "standard", label: "1.5 – 2.5" },
      { value: "high", label: "Over 2.5" },
    ],
  },
  {
    key: "sl_atr_bucket",
    label: "Stop width",
    options: [
      { value: "tight", label: "Tight" },
      { value: "normal", label: "Normal" },
      { value: "wide", label: "Wide" },
    ],
  },
  {
    key: "calendar_flag",
    label: "News day",
    options: [
      { value: "true", label: "High impact" },
      { value: "false", label: "Quiet" },
    ],
    parse: (raw) => raw === "true",
  },
  {
    key: "day_of_week",
    label: "Day",
    options: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].map((d) => ({
      value: d,
      label: d.charAt(0).toUpperCase() + d.slice(1),
    })),
  },
  {
    key: "htf_trend_state",
    label: "HTF trend",
    options: [
      { value: "up", label: "Up" },
      { value: "down", label: "Down" },
    ],
  },
  {
    key: "ema_slope_bucket",
    label: "EMA slope",
    options: [
      { value: "down", label: "Down" },
      { value: "flat", label: "Flat" },
      { value: "up", label: "Up" },
    ],
  },
  {
    key: "atr_change_bucket",
    label: "Vol change",
    options: [
      { value: "contracting", label: "Contracting" },
      { value: "stable", label: "Stable" },
      { value: "expanding", label: "Expanding" },
    ],
  },
  {
    key: "entry_convention",
    label: "Entry convention",
    options: [
      { value: "marked", label: "Marked" },
      { value: "next_open", label: "Next open" },
    ],
  },
  {
    key: "at_key_level",
    label: "At key level",
    options: [
      { value: "true", label: "Yes" },
      { value: "false", label: "No" },
    ],
    parse: (raw) => raw === "true",
  },
  {
    key: "consolidation_before",
    label: "Consolidation",
    options: [
      { value: "true", label: "Yes" },
      { value: "false", label: "No" },
    ],
    parse: (raw) => raw === "true",
  },
];

/** The operator's own read, recorded at resolution. */
const LABELS: Dimension[] = [
  { key: "observed_trend", label: "Trend (read)", options: toOptions(OBSERVED_TRENDS, OBSERVED_TREND_LABELS) },
  { key: "market_structure", label: "Structure", options: toOptions(MARKET_STRUCTURES, MARKET_STRUCTURE_LABELS) },
  { key: "htf_alignment", label: "HTF alignment", options: toOptions(HTF_ALIGNMENTS, HTF_ALIGNMENT_LABELS) },
  { key: "entry_quality", label: "Entry quality", options: toOptions(ENTRY_QUALITIES, ENTRY_QUALITY_LABELS) },
  {
    key: "confidence_min",
    label: "Confidence ≥",
    options: [1, 2, 3, 4, 5].map((n) => ({ value: String(n), label: String(n) })),
    parse: Number,
  },
];

const DIMENSIONS = [...COMPUTED, ...LABELS];

const CONFLUENCE_OPTIONS = toOptions(CONFLUENCE_TAGS, CONFLUENCE_LABELS);

/** Dimensions the server computes at the signal bar; they follow the cursor. */
const AUTO_FILLED = [
  "trend_state",
  "session",
  "atr_bucket",
  "rsi_band",
  "day_of_week",
  "htf_trend_state",
  "ema_slope_bucket",
  "atr_change_bucket",
] as const;

const DEFAULT_MIN_SAMPLES = Number(import.meta.env.VITE_MIN_SAMPLES) || 3;

const HELPER = "text-sm text-zinc-500";

/**
 * Every dimension is a plain string here, with ANY meaning "do not filter" —
 * Radix cannot hold "" as a value. The real types are recovered by each
 * dimension's `parse` when the payload is assembled.
 */
const dimension = () => z.string();

const schema = z.object({
  setup_id: z.string().min(1, "Pick a setup"),
  trend_state: dimension(),
  session: dimension(),
  atr_bucket: dimension(),
  rsi_band: dimension(),
  day_of_week: dimension(),
  htf_trend_state: dimension(),
  ema_slope_bucket: dimension(),
  atr_change_bucket: dimension(),
  entry_convention: dimension(),
  at_key_level: dimension(),
  consolidation_before: dimension(),
  side: dimension(),
  rr_bucket: dimension(),
  sl_atr_bucket: dimension(),
  calendar_flag: dimension(),
  observed_trend: dimension(),
  market_structure: dimension(),
  htf_alignment: dimension(),
  entry_quality: dimension(),
  confidence_min: dimension(),
  confluence_tags: z.array(z.string()),
  pinned: z.array(z.string()),
  min_samples: z.coerce.number().int().min(1, "At least one sample"),
  exclude_peeked: z.boolean(),
  exclude_assisted: z.boolean(),
  blinded_only: z.boolean(),
});

type FormValues = z.infer<typeof schema>;

const DEFAULTS: FormValues = {
  setup_id: "",
  trend_state: ANY,
  session: ANY,
  atr_bucket: ANY,
  rsi_band: ANY,
  day_of_week: ANY,
  htf_trend_state: ANY,
  ema_slope_bucket: ANY,
  atr_change_bucket: ANY,
  entry_convention: ANY,
  at_key_level: ANY,
  consolidation_before: ANY,
  side: ANY,
  rr_bucket: ANY,
  sl_atr_bucket: ANY,
  calendar_flag: ANY,
  observed_trend: ANY,
  market_structure: ANY,
  htf_alignment: ANY,
  entry_quality: ANY,
  confidence_min: ANY,
  confluence_tags: [],
  pinned: [],
  min_samples: DEFAULT_MIN_SAMPLES,
  exclude_peeked: true,
  exclude_assisted: false,
  blinded_only: false,
};

interface ComparePanelProps {
  session: Session | null;
  /** Blinded sessions withhold the base rate until the trade has resolved. */
  blinded?: boolean;
}

export function ComparePanel({ session, blinded = false }: ComparePanelProps) {
  // The marked levels live on the page-level form, so the planned R:R comes
  // straight from there rather than being threaded down as a prop.
  const markForm = useFormContext() as MarkTradeForm;
  const [markedEntry, markedSl, markedTp] = useWatch({
    control: markForm.control,
    name: ["entry", "sl", "tp"],
  });
  const setupOptions = useSetupOptions();
  const compare = useCompare();
  const currentBar = useCurrentBar();
  // Primitives, not the store object: updateLive fires every bar during an
  // active trade and this panel has no reason to re-render for it.
  const tradeStatus = useActiveTradeStore((s) => s.status);
  const armedSetup = useActiveTradeStore((s) => s.setup_id);
  const armedSide = useActiveTradeStore((s) => s.side);

  const [showLabels, setShowLabels] = useState(false);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: DEFAULTS,
  });

  const confluence = form.watch("confluence_tags");

  /**
   * Auto-fill writes only to fields the operator has not touched, and without
   * `shouldDirty`, so filled values stay clean and keep tracking the cursor
   * while a deliberate override is never clobbered.
   */
  const setIfClean = (name: keyof FormValues, value: string) => {
    if (form.formState.dirtyFields[name]) return;
    form.setValue(name, value as never);
  };

  const signalTs = currentBar ? toUtcIso(currentBar.ts) : null;
  const { data: signalContext } = useSignalContext(
    session?.symbol ?? "",
    session?.timeframe ?? "",
    signalTs,
  );
  const { data: candleBounds } = useCandleBounds(session?.symbol ?? "", session?.timeframe ?? "");
  const pinnedDims = form.watch("pinned");
  const htfTrendFilter = form.watch("htf_trend_state");
  const htfMissing =
    candleBounds?.htf_available === false &&
    !signalContext?.htf_trend_state &&
    (pinnedDims.includes("htf_trend_state") ||
      (!!htfTrendFilter && htfTrendFilter !== ANY));

  // Follow the cursor: the computed dimensions describe the bar being looked at,
  // so moving the cursor should move them rather than leave a stale reading.
  useEffect(() => {
    if (!signalContext) return;
    for (const key of AUTO_FILLED) {
      const value = signalContext[key];
      if (value != null) setIfClean(key, String(value));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signalContext]);

  // A trade being marked is the thing you want to compare against.
  useEffect(() => {
    if (tradeStatus === "idle" || !armedSetup) return;
    setIfClean("setup_id", armedSetup);
    setIfClean("side", String(armedSide));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tradeStatus, armedSetup, armedSide]);

  const markedRr = useMemo(() => {
    const entry = toLevel(markedEntry);
    const sl = toLevel(markedSl);
    const tp = toLevel(markedTp);
    if (entry == null || sl == null || tp == null) return null;
    return riskReward(tp > entry ? 1 : -1, entry, sl, tp);
  }, [markedEntry, markedSl, markedTp]);

  useEffect(() => {
    if (markedRr == null) return;
    setIfClean("rr_bucket", rrBucket(markedRr));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [markedRr]);

  const sideValue = form.watch("side");

  /**
   * In a blinded session the prior stays hidden until the trade has resolved, so
   * the decision is committed before the model's own answer is on screen. The
   * feedback still arrives in the same session — withholding it forever would
   * make the lab a data-entry exercise rather than a training loop.
   */
  const baseRateWithheld = blinded && tradeStatus !== "resolved";

  /**
   * The base rate describes the bar, so it follows the cursor with no button.
   * Marked levels are absolute prices and the store indexes barrier distance in
   * ATR, so they are converted and snapped onto the ladder it can answer for;
   * with nothing marked yet it falls back to a 1.5R-on-1-ATR read.
   */
  const baseRateQuery = useMemo<BaseRateQuery | null>(() => {
    if (baseRateWithheld) return null;
    if (!session?.symbol || !session.timeframe || !signalTs) return null;

    const atr = signalContext?.atr_at_signal ?? null;
    const entry = toLevel(markedEntry);
    const sl = toLevel(markedSl);
    const tp = toLevel(markedTp);

    let side = sideValue && sideValue !== ANY ? Number(sideValue) : null;
    if (side == null && entry != null && tp != null) side = tp > entry ? 1 : -1;

    return {
      symbol: session.symbol,
      timeframe: session.timeframe,
      signalTs,
      horizon: MAX_BARS,
      stopAtr:
        atr && entry != null && sl != null ? snapToTouchLevel(Math.abs(entry - sl) / atr) : 1.0,
      targetAtr:
        atr && entry != null && tp != null ? snapToTouchLevel(Math.abs(tp - entry) / atr) : 1.5,
      side: side ?? 1,
    };
  }, [
    baseRateWithheld,
    session?.symbol,
    session?.timeframe,
    signalTs,
    signalContext?.atr_at_signal,
    markedEntry,
    markedSl,
    markedTp,
    sideValue,
  ]);

  const baseRate = useBaseRate(baseRateQuery);

  const onSubmit = form.handleSubmit((values) => {
    if (!session?.symbol || !session.timeframe) return;

    const context: Record<string, unknown> = {};
    for (const dim of DIMENSIONS) {
      const raw = values[dim.key];
      if (!raw || raw === ANY) continue;
      context[dim.key] = dim.parse ? dim.parse(raw) : raw;
    }
    if (values.confluence_tags.length > 0) context.confluence_tags = values.confluence_tags;

    compare.mutate({
      setup_id: values.setup_id,
      symbol: session.symbol,
      timeframe: session.timeframe,
      context: context as CompareContext,
      pinned: values.pinned,
      min_samples: values.min_samples,
      exclude_peeked: values.exclude_peeked,
      exclude_assisted: values.exclude_assisted,
      blinded_only: values.blinded_only,
    });
  });

  const result = compare.data;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-zinc-400">Compare</CardTitle>
        <p className={HELPER}>
          How this setup has resolved in a matching context. Pinned dimensions are never
          relaxed — if they can&apos;t be met you get no signal, not a wider sample.
        </p>
      </CardHeader>
      <CardContent>
        <Form {...form}>
          <form onSubmit={onSubmit} className="space-y-3">
            <ComboboxField
              control={form.control}
              name="setup_id"
              label="Setup"
              options={setupOptions}
              placeholder="Select setup"
              searchPlaceholder="Search patterns…"
              emptyText="No setup found."
            />

            <InputField
              control={form.control}
              name="min_samples"
              label="Min samples"
              type="number"
              min={1}
              step={1}
              description="Win rate is withheld below this count. Lower during labelling; production default is 30."
            />

            <div className="grid grid-cols-2 gap-2">
              {COMPUTED.map((dim) => (
                <DimensionField key={dim.key} form={form} dimension={dim} />
              ))}
            </div>

            {signalContext?.context_reliable === false && (
              <p className={HELPER}>
                Only {signalContext.warmup_bars_available} bars of history at this point — the
                computed context is unreliable here.
              </p>
            )}

            {htfMissing && (
              <p className={HELPER}>
                {candleBounds?.htf_timeframe ?? "Higher timeframe"} candles are not ingested — HTF
                trend filter is pinned but unavailable for this bar.
              </p>
            )}

            {baseRateWithheld ? (
              <p className={HELPER}>
                Blinded session — the context base rate stays hidden until this trade resolves,
                so the label records your read rather than the model&apos;s.
              </p>
            ) : (
              <BaseRateBlock
                query={baseRateQuery}
                result={baseRate.data ?? null}
                error={baseRate.error}
                isLoading={baseRate.isLoading}
                setupWinRate={result?.win_rate ?? null}
              />
            )}

            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-expanded={showLabels}
              onClick={() => setShowLabels((s) => !s)}
              className={cn("h-auto justify-start gap-1 px-0 font-normal", HELPER)}
            >
              {showLabels ? (
                <ChevronDown className="h-3 w-3" aria-hidden="true" />
              ) : (
                <ChevronRight className="h-3 w-3" aria-hidden="true" />
              )}
              Your labels {confluence.length > 0 && `· ${confluence.length} confluence`}
            </Button>

            {showLabels && (
              <div className="space-y-2 rounded border border-zinc-800 p-2">
                <div className="grid grid-cols-2 gap-2">
                  {LABELS.map((dim) => (
                    <DimensionField key={dim.key} form={form} dimension={dim} />
                  ))}
                </div>

                <ChipToggleField
                  control={form.control}
                  name="confluence_tags"
                  label="Confluence"
                  options={CONFLUENCE_OPTIONS}
                  action={<PinToggle form={form} name="confluence_tags" />}
                  description={
                    confluence.length > 1 ? "Matches occurrences carrying all of these." : undefined
                  }
                />

                <div className="space-y-2 pt-1">
                  <SwitchField
                    control={form.control}
                    name="exclude_peeked"
                    label="Exclude labels that saw ahead"
                    labelClassName="text-xs"
                  />
                  <SwitchField
                    control={form.control}
                    name="exclude_assisted"
                    label="Exclude labels that saw the base rate"
                    labelClassName="text-xs"
                  />
                  <SwitchField
                    control={form.control}
                    name="blinded_only"
                    label="Blinded sessions only"
                    labelClassName="text-xs"
                  />
                </div>
              </div>
            )}

            <Button type="submit" className="w-full" disabled={!session || compare.isPending}>
              {compare.isPending ? "Comparing…" : "Run compare"}
            </Button>
            {compare.isError && <p className={HELPER}>{compare.error.message}</p>}

            {result && (
              <CompareResultView
                result={result}
                markedRr={markedRr}
                side={(sideValue && sideValue !== ANY ? Number(sideValue) : baseRateQuery?.side ?? 1) as 1 | -1}
                baseRateWinRate={baseRate.data?.win_rate ?? null}
                stopAtr={baseRateQuery?.stopAtr ?? 1.0}
                targetAtr={baseRateQuery?.targetAtr ?? 1.5}
              />
            )}
          </form>
        </Form>
      </CardContent>
    </Card>
  );
}

/**
 * The prior the setup has to beat. Its sample is every bar that ever shared this
 * context, not the handful of trades labelled so far, so it is usually the only
 * number on screen with a real sample behind it — and the gap between the two is
 * the part that means something.
 */
function BaseRateBlock({
  query,
  result,
  error,
  isLoading,
  setupWinRate,
}: {
  query: BaseRateQuery | null;
  result: BaseRate | null;
  error: Error | null;
  isLoading: boolean;
  setupWinRate: number | null;
}) {
  const markSeen = useReplayStore((s) => s.markBaseRateSeen);
  const shown = !!result && result.level_used !== "no_signal";

  // Exposure is recorded the moment a real number reaches the screen, so the
  // occurrence carries `assisted` whether or not the operator acts on it.
  useEffect(() => {
    if (shown) markSeen();
  }, [shown, markSeen]);

  if (!query) return null;

  const noSignal = result?.level_used === "no_signal";
  const delta =
    result?.win_rate != null && setupWinRate != null ? setupWinRate - result.win_rate : null;
  const grid = (result?.target_grid ?? []).filter((c) => c.decided > 0);
  const bestCell = grid.reduce<(typeof grid)[number] | null>(
    (acc, c) => (acc == null || (c.expectancy_r ?? -99) > (acc.expectancy_r ?? -99) ? c : acc),
    null,
  );

  return (
    <div className="space-y-2 rounded border border-zinc-800 p-2">
      <p className={HELPER}>
        Context base rate — what price did next from every past bar in this context, with no
        setup involved. {query.targetAtr}× ATR target against a {query.stopAtr}× ATR stop over{" "}
        {query.horizon} bars, {query.side === 1 ? "long" : "short"}.
      </p>

      {isLoading && <p className={HELPER}>Loading…</p>}
      {error && <p className={HELPER}>{error.message}</p>}

      {result && noSignal && (
        <p className={HELPER}>
          Not enough resolved bars in this context (need {result.min_samples_required}, have{" "}
          {result.decided_available}).
        </p>
      )}

      {result && !noSignal && (
        <>
          <RecommendationBanner
            recommendation={deriveRecommendation({
              side: query.side as 1 | -1,
              winRate: result.win_rate,
              wilsonLow: result.wilson_low,
              wilsonHigh: result.wilson_high,
              expectancyR: result.expectancy_r_net ?? result.expectancy_r,
              decided: result.decided,
              minSamples: result.min_samples_required ?? 200,
              levelUsed: result.level_used,
              effectiveN: result.effective_n,
              breakEvenWinRate: breakEvenFromGeometry(query.stopAtr ?? 1.0, query.targetAtr ?? 1.5),
              setupDelta: delta,
            })}
          />
          <div className="grid grid-cols-2 gap-2">
            <StatCard
              title="Base rate"
              value={formatPercent(result.win_rate)}
              subtitle={`n=${result.decided} · ~${Math.round(result.effective_n ?? 0)} independent`}
            />
            <StatCard
              title="Wilson CI"
              value={
                result.wilson_low != null
                  ? `${formatPercent(result.wilson_low)} – ${formatPercent(result.wilson_high)}`
                  : "—"
              }
              // Adjacent bars share all but one of their forward bars, so the
              // row count is not a count of independent draws.
              subtitle="widened to independent bars"
            />
            <StatCard
              title="Expectancy"
              value={(result.expectancy_r_net ?? result.expectancy_r)?.toFixed(2) ?? "—"}
              subtitle={
                result.expectancy_r_net != null &&
                result.expectancy_r != null &&
                result.expectancy_r_net !== result.expectancy_r
                  ? `net · gross ${result.expectancy_r.toFixed(2)}R`
                  : "in R"
              }
            />
            <StatCard
              title="Typical path"
              value={
                result.median_mfe_atr != null
                  ? `+${result.median_mfe_atr.toFixed(2)} / ${result.median_mae_atr?.toFixed(2) ?? "—"}`
                  : "—"
              }
              subtitle="median peak / dip, ATR"
            />
          </div>

          {grid.length > 0 && (
            <div className="space-y-1">
              <p className={HELPER}>
                Same {query.stopAtr}× ATR stop, different targets — the store prices every one
                from the same bars.
              </p>
              <div className="overflow-x-auto">
                <table className="tnum w-full font-mono text-sm text-zinc-500">
                  <thead>
                    <tr>
                      <th className="py-0.5 text-left font-normal">Target</th>
                      <th className="py-0.5 text-right font-normal">Win</th>
                      <th className="py-0.5 text-right font-normal">Exp</th>
                      <th className="py-0.5 text-right font-normal">n</th>
                    </tr>
                  </thead>
                  <tbody>
                    {grid.map((cell) => (
                      <tr
                        key={cell.target_atr}
                        title={cell === bestCell ? "Best expectancy in this context" : undefined}
                      >
                        <td className="py-0.5">
                          {cell.target_atr.toFixed(1)}× ATR{cell === bestCell ? " *" : ""}
                        </td>
                        <td className="py-0.5 text-right">{formatPercent(cell.win_rate)}</td>
                        <td className="py-0.5 text-right">
                          {cell.expectancy_r?.toFixed(2) ?? "—"}
                        </td>
                        <td className="py-0.5 text-right">{cell.decided}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <p className={HELPER}>
            Matched on{" "}
            {result.dimensions_used.length > 0
              ? result.dimensions_used.join(", ")
              : "no context filters"}
            .
          </p>

          {delta != null && (
            <p className={HELPER}>
              Your setup is {delta > 0 ? "+" : ""}
              {(delta * 100).toFixed(1)} points against this base rate — that gap is the edge,
              not the win rate on its own.
            </p>
          )}
        </>
      )}
    </div>
  );
}

/** Pins live on the form too, so there is one source for what gets submitted. */
function PinToggle({
  form,
  name,
}: {
  form: UseFormReturn<FormValues>;
  name: DimensionKey | "confluence_tags";
}) {
  const pinned = form.watch("pinned").includes(name);
  const toggle = () => {
    const current = form.getValues("pinned");
    form.setValue(
      "pinned",
      pinned ? current.filter((p) => p !== name) : [...current, name],
      { shouldDirty: true },
    );
  };

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      aria-pressed={pinned}
      title={pinned ? "Pinned — never relaxed" : "Pin so this is never relaxed"}
      onClick={toggle}
      className={cn("h-5 w-5", pinned ? "text-operator" : "text-zinc-600 hover:text-zinc-400")}
    >
      <Pin className={cn("h-3 w-3", pinned && "fill-current")} aria-hidden="true" />
    </Button>
  );
}

function DimensionField({
  form,
  dimension,
}: {
  form: UseFormReturn<FormValues>;
  dimension: Dimension;
}) {
  return (
    <SelectField
      control={form.control}
      name={dimension.key}
      label={dimension.label}
      action={<PinToggle form={form} name={dimension.key} />}
      options={[{ value: ANY, label: "Any" }, ...dimension.options]}
    />
  );
}

function CompareResultView({
  result,
  markedRr,
  side,
  baseRateWinRate,
  stopAtr,
  targetAtr,
}: {
  result: import("@/types").CompareResult;
  markedRr: number | null;
  side: 1 | -1;
  baseRateWinRate: number | null;
  stopAtr: number;
  targetAtr: number;
}) {
  const noSignal = result.level_used === "no_signal";
  const grid = result.target_grid.filter((t) => t.decided > 0);
  const best = grid.reduce<(typeof grid)[number] | null>(
    (acc, t) => (acc == null || (t.expectancy_r ?? -99) > (acc.expectancy_r ?? -99) ? t : acc),
    null,
  );
  const breakEven = markedRr != null ? breakEvenFromRr(markedRr) : breakEvenFromGeometry(stopAtr, targetAtr);
  const setupDelta =
    result.win_rate != null && baseRateWinRate != null ? result.win_rate - baseRateWinRate : null;
  const topSkipReason =
    result.skip_reasons && Object.keys(result.skip_reasons).length > 0
      ? Object.entries(result.skip_reasons).sort((a, b) => b[1] - a[1])[0]
      : null;

  return (
    <div className="space-y-3">
      {noSignal &&
        result.min_samples_required != null &&
        result.decided_available != null && (
          <p className={HELPER}>
            Not enough decided trades (need {result.min_samples_required}, have{" "}
            {result.decided_available}). Skips and timeouts don&apos;t count.
          </p>
        )}
      {!noSignal && (
        <RecommendationBanner
          recommendation={deriveRecommendation({
            side,
            winRate: result.win_rate,
            wilsonLow: result.wilson_low,
            wilsonHigh: result.wilson_high,
            expectancyR: result.expectancy_r_net ?? result.expectancy_r,
            decided: result.decided,
            minSamples: result.min_samples_required ?? 30,
            levelUsed: result.level_used,
            overlapRatio: result.overlap_ratio,
            breakEvenWinRate: breakEven,
            setupDelta,
          })}
        />
      )}
      <div className="grid grid-cols-2 gap-2">
        <StatCard
          title="Win rate"
          value={noSignal ? "No signal" : formatPercent(result.win_rate)}
          subtitle={`n=${result.decided} · ${result.level_used}`}
        />
        <StatCard
          title="Wilson CI"
          value={
            result.wilson_low != null
              ? `${formatPercent(result.wilson_low)} – ${formatPercent(result.wilson_high)}`
              : "—"
          }
          // The interval assumes independent draws. Trades held over the same
          // bars are not independent, so a high overlap means it is too tight.
          subtitle={
            result.overlap_ratio != null && result.overlap_ratio > 0
              ? `${formatPercent(result.overlap_ratio)} overlapping — CI optimistic`
              : undefined
          }
        />
        <StatCard
          title="Expectancy"
          value={(result.expectancy_r_net ?? result.expectancy_r)?.toFixed(2) ?? "—"}
          subtitle={
            result.expectancy_r_net != null &&
            result.expectancy_r != null &&
            result.expectancy_r_net !== result.expectancy_r
              ? `net · gross ${result.expectancy_r.toFixed(2)}R`
              : "in R"
          }
        />
        <StatCard
          title="Typical path"
          value={
            result.median_mfe_r != null
              ? `+${result.median_mfe_r.toFixed(2)} / ${result.median_mae_r?.toFixed(2) ?? "—"}`
              : "—"
          }
          subtitle="median peak / dip, R"
        />
      </div>

      {grid.length > 0 && (
        <div className="space-y-1">
          <p className={HELPER}>
            Same stops, different targets{markedRr != null && ` — you marked ${markedRr.toFixed(2)}R`}
          </p>
          <div className="overflow-x-auto">
            <table className="tnum w-full font-mono text-sm text-zinc-500">
              <thead>
                <tr>
                  <th className="py-0.5 text-left font-normal">Target</th>
                  <th className="py-0.5 text-right font-normal">Win</th>
                  <th className="py-0.5 text-right font-normal">Exp</th>
                  <th className="py-0.5 text-right font-normal">n</th>
                </tr>
              </thead>
              <tbody>
                {grid.map((row) => (
                  <tr
                    key={row.target_r}
                    title={row === best ? "Best expectancy across the matched set" : undefined}
                  >
                    <td className="py-0.5">
                      {row.target_r.toFixed(1)}R{row === best ? " *" : ""}
                    </td>
                    <td className="py-0.5 text-right">{formatPercent(row.win_rate)}</td>
                    <td className="py-0.5 text-right">{row.expectancy_r?.toFixed(2) ?? "—"}</td>
                    <td className="py-0.5 text-right">{row.decided}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {(result.skipped_count > 0 || result.excluded_peeked > 0) && (
        <p className={HELPER}>
          {result.skipped_count > 0 && (
            <>
              Passed on {result.skipped_count}
              {result.skip_rate != null && ` (${formatPercent(result.skip_rate)} of similar setups)`}
              {topSkipReason && (
                <>
                  {" "}
                  — top reason:{" "}
                  {SKIP_REASON_LABELS[topSkipReason[0] as keyof typeof SKIP_REASON_LABELS] ??
                    topSkipReason[0]}
                </>
              )}
              {Object.keys(result.skip_reasons).length > 1 && (
                <>
                  {" "}
                  (
                  {Object.entries(result.skip_reasons)
                    .map(
                      ([reason, count]) =>
                        `${SKIP_REASON_LABELS[reason as keyof typeof SKIP_REASON_LABELS] ?? reason} ${count}`,
                    )
                    .join(", ")}
                  )
                </>
              )}
              .{" "}
            </>
          )}
          {result.excluded_peeked > 0 && `${result.excluded_peeked} excluded for seeing ahead.`}
        </p>
      )}
    </div>
  );
}
