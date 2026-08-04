import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Pin } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { StatCard } from "@/components/common/StatCard";
import type { PriceLevels } from "@/components/chart/PriceLines";
import { useSignalContext } from "@/hooks/useCandles";
import { useCompare } from "@/hooks/useCompare";
import { useCurrentBar } from "@/hooks/useReplay";
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
import { useActiveTradeStore } from "@/stores/activeTradeStore";
import { cn } from "@/lib/utils";
import type { CompareContext, Session } from "@/types";

/** Radix Select has no empty-string value, so "any" needs a sentinel. */
const ANY = "__any";

interface Dimension {
  key: keyof CompareContext;
  label: string;
  options: { value: string; label: string }[];
  /** Selects hold strings; the API wants the real type. */
  parse?: (raw: string) => unknown;
}

const opts = <T extends string>(values: readonly T[], labels: Record<T, string>) =>
  values.map((v) => ({ value: v, label: labels[v] }));

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
];

/** The operator's own read, recorded at resolution. */
const LABELS: Dimension[] = [
  { key: "observed_trend", label: "Trend (read)", options: opts(OBSERVED_TRENDS, OBSERVED_TREND_LABELS) },
  { key: "market_structure", label: "Structure", options: opts(MARKET_STRUCTURES, MARKET_STRUCTURE_LABELS) },
  { key: "htf_alignment", label: "HTF alignment", options: opts(HTF_ALIGNMENTS, HTF_ALIGNMENT_LABELS) },
  { key: "entry_quality", label: "Entry quality", options: opts(ENTRY_QUALITIES, ENTRY_QUALITY_LABELS) },
  {
    key: "confidence_min",
    label: "Confidence ≥",
    options: [1, 2, 3, 4, 5].map((n) => ({ value: String(n), label: String(n) })),
    parse: Number,
  },
];

const AUTO_FILLED = ["trend_state", "session", "atr_bucket", "rsi_band"] as const;

const DEFAULT_MIN_SAMPLES = Number(import.meta.env.VITE_MIN_SAMPLES) || 3;

const HELPER = "text-sm text-zinc-500";

interface ComparePanelProps {
  session: Session | null;
  levels?: PriceLevels;
}

export function ComparePanel({ session, levels }: ComparePanelProps) {
  const setupOptions = useSetupOptions();
  const compare = useCompare();
  const currentBar = useCurrentBar();
  // Primitives, not the store object: updateLive fires every bar during an
  // active trade and this panel has no reason to re-render for it.
  const tradeStatus = useActiveTradeStore((s) => s.status);
  const armedSetup = useActiveTradeStore((s) => s.setup_id);
  const armedSide = useActiveTradeStore((s) => s.side);

  const [setupId, setSetupId] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [confluence, setConfluence] = useState<string[]>([]);
  const [pins, setPins] = useState<string[]>([]);
  const [showLabels, setShowLabels] = useState(false);
  const [excludePeeked, setExcludePeeked] = useState(true);
  const [blindedOnly, setBlindedOnly] = useState(false);
  const [minSamples, setMinSamples] = useState(DEFAULT_MIN_SAMPLES);

  const signalTs = currentBar ? toUtcIso(currentBar.ts) : null;
  const { data: signalContext } = useSignalContext(
    session?.symbol ?? "",
    session?.timeframe ?? "",
    signalTs,
  );

  // Follow the cursor: the computed dimensions describe the bar being looked at,
  // so moving the cursor should move them rather than leave a stale reading.
  useEffect(() => {
    if (!signalContext) return;
    setValues((prev) => ({
      ...prev,
      ...Object.fromEntries(AUTO_FILLED.map((k) => [k, signalContext[k]])),
    }));
  }, [signalContext]);

  // A trade being marked is the thing you want to compare against.
  useEffect(() => {
    if (tradeStatus === "idle" || !armedSetup) return;
    setSetupId(armedSetup);
    setValues((prev) => ({ ...prev, side: String(armedSide) }));
  }, [tradeStatus, armedSetup, armedSide]);

  const markedRr = useMemo(() => {
    if (!levels?.entry || !levels.sl || !levels.tp) return null;
    const side = levels.tp > levels.entry ? 1 : -1;
    return riskReward(side, levels.entry, levels.sl, levels.tp);
  }, [levels]);

  useEffect(() => {
    if (markedRr == null) return;
    setValues((prev) => ({ ...prev, rr_bucket: rrBucket(markedRr) }));
  }, [markedRr]);

  const set = (key: string, raw: string) =>
    setValues((prev) => {
      if (raw === ANY) {
        const { [key]: _dropped, ...rest } = prev;
        return rest;
      }
      return { ...prev, [key]: raw };
    });

  const togglePin = (key: string) =>
    setPins((prev) => (prev.includes(key) ? prev.filter((p) => p !== key) : [...prev, key]));

  const toggleTag = (tag: string) =>
    setConfluence((prev) => (prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag]));

  const onSubmit = () => {
    if (!session?.symbol || !session.timeframe || !setupId) return;

    const context: Record<string, unknown> = {};
    for (const dim of [...COMPUTED, ...LABELS]) {
      const raw = values[dim.key];
      if (raw == null || raw === "") continue;
      context[dim.key] = dim.parse ? dim.parse(raw) : raw;
    }
    if (confluence.length > 0) context.confluence_tags = confluence;

    compare.mutate({
      setup_id: setupId,
      symbol: session.symbol,
      timeframe: session.timeframe,
      context: context as CompareContext,
      pinned: pins,
      min_samples: minSamples,
      exclude_peeked: excludePeeked,
      blinded_only: blindedOnly,
    });
  };

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
      <CardContent className="space-y-3">
        <div className="space-y-1.5">
          <Label>Setup</Label>
          <Combobox
            options={setupOptions}
            value={setupId}
            onChange={setSetupId}
            placeholder="Select setup"
            searchPlaceholder="Search patterns…"
            emptyText="No setup found."
          />
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="cmp-min-samples">Min samples</Label>
          <Input
            id="cmp-min-samples"
            type="number"
            min={1}
            step={1}
            value={minSamples}
            onChange={(e) => {
              const n = parseInt(e.target.value, 10);
              setMinSamples(Number.isFinite(n) && n >= 1 ? n : 1);
            }}
          />
          <p className={HELPER}>
            Win rate is withheld below this count. Lower during labelling; production default is 30.
          </p>
        </div>

        <div className="grid grid-cols-2 gap-2">
          {COMPUTED.map((dim) => (
            <DimensionField
              key={dim.key}
              dimension={dim}
              value={values[dim.key] ?? ANY}
              onChange={(v) => set(dim.key, v)}
              pinned={pins.includes(dim.key)}
              onTogglePin={() => togglePin(dim.key)}
            />
          ))}
        </div>

        {signalContext?.context_reliable === false && (
          <p className={HELPER}>
            Only {signalContext.warmup_bars_available} bars of history at this point — the
            computed context is unreliable here.
          </p>
        )}

        <button
          type="button"
          onClick={() => setShowLabels((s) => !s)}
          className={cn("flex items-center gap-1", HELPER)}
        >
          {showLabels ? (
            <ChevronDown className="h-3 w-3" aria-hidden="true" />
          ) : (
            <ChevronRight className="h-3 w-3" aria-hidden="true" />
          )}
          Your labels {confluence.length > 0 && `· ${confluence.length} confluence`}
        </button>

        {showLabels && (
          <div className="space-y-2 rounded border border-zinc-800 p-2">
            <div className="grid grid-cols-2 gap-2">
              {LABELS.map((dim) => (
                <DimensionField
                  key={dim.key}
                  dimension={dim}
                  value={values[dim.key] ?? ANY}
                  onChange={(v) => set(dim.key, v)}
                  pinned={pins.includes(dim.key)}
                  onTogglePin={() => togglePin(dim.key)}
                />
              ))}
            </div>

            <div className="space-y-1.5">
              <div className="flex items-center gap-1">
                <Label>Confluence</Label>
                <PinButton
                  pinned={pins.includes("confluence_tags")}
                  onClick={() => togglePin("confluence_tags")}
                />
              </div>
              <div className="flex flex-wrap gap-1.5">
                {CONFLUENCE_TAGS.map((tag) => (
                  <button
                    key={tag}
                    type="button"
                    onClick={() => toggleTag(tag)}
                    className={cn(
                      "rounded border px-2 py-0.5 text-xs transition-colors",
                      confluence.includes(tag)
                        ? "border-operator bg-operator/10 text-operator"
                        : "border-zinc-800 text-zinc-400 hover:border-zinc-600",
                    )}
                  >
                    {CONFLUENCE_LABELS[tag]}
                  </button>
                ))}
              </div>
              {confluence.length > 1 && (
                <p className={HELPER}>Matches occurrences carrying all of these.</p>
              )}
            </div>

            <div className="flex items-center gap-2 pt-1">
              <Switch id="cmp-peeked" checked={excludePeeked} onCheckedChange={setExcludePeeked} />
              <Label htmlFor="cmp-peeked" className="cursor-pointer text-xs">
                Exclude labels that saw ahead
              </Label>
            </div>
            <div className="flex items-center gap-2">
              <Switch id="cmp-blinded" checked={blindedOnly} onCheckedChange={setBlindedOnly} />
              <Label htmlFor="cmp-blinded" className="cursor-pointer text-xs">
                Blinded sessions only
              </Label>
            </div>
          </div>
        )}

        <Button
          type="button"
          className="w-full"
          disabled={!session || !setupId || compare.isPending}
          onClick={onSubmit}
        >
          {compare.isPending ? "Comparing…" : "Run compare"}
        </Button>
        {compare.isError && <p className={HELPER}>{compare.error.message}</p>}

        {result && <CompareResultView result={result} markedRr={markedRr} />}
      </CardContent>
    </Card>
  );
}

function PinButton({ pinned, onClick }: { pinned: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={pinned}
      title={pinned ? "Pinned — never relaxed" : "Pin so this is never relaxed"}
      className={cn(
        "rounded p-0.5 transition-colors",
        pinned ? "text-operator" : "text-zinc-600 hover:text-zinc-400",
      )}
    >
      <Pin className={cn("h-3 w-3", pinned && "fill-current")} aria-hidden="true" />
    </button>
  );
}

interface DimensionFieldProps {
  dimension: Dimension;
  value: string;
  onChange: (value: string) => void;
  pinned: boolean;
  onTogglePin: () => void;
}

function DimensionField({ dimension, value, onChange, pinned, onTogglePin }: DimensionFieldProps) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1">
        <Label>{dimension.label}</Label>
        <PinButton pinned={pinned} onClick={onTogglePin} />
      </div>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ANY}>Any</SelectItem>
          {dimension.options.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              {o.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function CompareResultView({
  result,
  markedRr,
}: {
  result: import("@/types").CompareResult;
  markedRr: number | null;
}) {
  const noSignal = result.level_used === "no_signal";
  const grid = result.target_grid.filter((t) => t.decided > 0);
  const best = grid.reduce<(typeof grid)[number] | null>(
    (acc, t) => (acc == null || (t.expectancy_r ?? -99) > (acc.expectancy_r ?? -99) ? t : acc),
    null,
  );

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
        <StatCard title="Expectancy" value={result.expectancy_r?.toFixed(2) ?? "—"} subtitle="in R" />
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
              {Object.keys(result.skip_reasons).length > 0 && (
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
