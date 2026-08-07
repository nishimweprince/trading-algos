import { useEffect, useState } from "react";
import { Crosshair } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import {
  ComboboxField,
  SelectField,
} from "@/components/common/fields";
import { LEVEL_LABELS, inferSide, type PriceLevelKey } from "@/components/chart/PriceLines";
import type { ReplayChartHandle } from "@/components/chart/ReplayChart";
import { TradeResolutionForm } from "@/components/trade/TradeResolutionForm";
import { SkipRecordBlock } from "@/components/trade/SkipRecordBlock";
import { captureProvenance, getCandleAt, useReplayStore, useCurrentBar } from "@/hooks/useReplay";
import {
  EMPTY_MARK_TRADE,
  toLevel,
  type MarkTradeForm,
  type MarkTradeParsed,
} from "@/hooks/useMarkTradeForm";
import { useSetupOptions, useSetups } from "@/hooks/useSetups";
import { useSubmitTrade } from "@/hooks/useTrades";
import { formatTs, toUtcIso } from "@/lib/format";
import { riskReward } from "@/lib/pips";
import { uploadScreenshot } from "@/lib/screenshot";
import { useSignalStore } from "@/stores/signalStore";
import { useActiveTradeStore } from "@/stores/activeTradeStore";
import { cn } from "@/lib/utils";
import type { Session } from "@/types";
import { useFormContext } from "react-hook-form";

const SIDE_OPTIONS = [
  { value: "1", label: "Long" },
  { value: "-1", label: "Short" },
];

interface TradeFormProps {
  session: Session | null;
  blinded?: boolean;
  armed?: PriceLevelKey | null;
  onArm?: (field: PriceLevelKey | null) => void;
  chartRef?: React.RefObject<ReplayChartHandle | null>;
  onTradeSaved?: () => void;
}

export function TradeForm({
  session,
  blinded,
  armed = null,
  onArm,
  chartRef,
  onTradeSaved,
}: TradeFormProps) {
  // The form lives on ReplayPage — see useMarkTradeForm for why.
  const form = useFormContext() as MarkTradeForm;
  const currentBar = useCurrentBar();
  const cursor = useReplayStore((s) => s.cursor);
  const candles = useReplayStore((s) => s.candles);
  const loadedStartOrdinal = useReplayStore((s) => s.loadedStartOrdinal);
  const signalBookmarkIdx = useReplayStore((s) => s.signalBookmarkIdx);
  const signalBookmarkTs = useReplayStore((s) => s.signalBookmarkTs);
  const activeSignalId = useSignalStore((s) => s.activeSignalId);
  const signalAnnotations = useSignalStore((s) => s.annotations);
  const setupOptions = useSetupOptions();
  const { data: setups = [] } = useSetups();
  const submitTrade = useSubmitTrade();
  const tradeStatus = useActiveTradeStore((s) => s.status);
  const startTrade = useActiveTradeStore((s) => s.startTrade);
  const draftTradeId = useActiveTradeStore((s) => s.draftTradeId);
  const noteLevelRevision = useActiveTradeStore((s) => s.noteLevelRevision);
  const [starting, setStarting] = useState(false);

  const entryRaw = form.watch("entry");
  const slRaw = form.watch("sl");
  const tpRaw = form.watch("tp");
  const setupId = form.watch("setup_id");
  const side = form.watch("side");
  // RHF's own dirty tracking decides whether side was chosen deliberately;
  // inference must not override a deliberate choice.
  const sideTouched = !!form.formState.dirtyFields.side;

  useEffect(() => {
    const subscription = form.watch((_values, { name }) => {
      if (name === "entry" || name === "sl" || name === "tp") noteLevelRevision();
    });
    return () => subscription.unsubscribe();
  }, [form, noteLevelRevision]);

  // Direction is implied by where the target sits relative to entry.
  useEffect(() => {
    if (sideTouched) return;
    const entry = toLevel(entryRaw);
    const tp = toLevel(tpRaw);
    if (entry != null && tp != null) form.setValue("side", inferSide(entry, tp));
  }, [entryRaw, tpRaw, sideTouched, form]);

  useEffect(() => {
    if (sideTouched) return;
    const setup = setups.find((s) => s.setup_id === setupId);
    if (setup?.default_side === 1 || setup?.default_side === -1) {
      form.setValue("side", setup.default_side);
    }
  }, [setupId, setups, sideTouched, form]);

  const clearForm = () => form.reset(EMPTY_MARK_TRADE);

  const buildSubmitPayload = (values: MarkTradeParsed) => ({
    session_id: session!.session_id,
    signal_id: activeSignalId ?? undefined,
    symbol: session!.symbol!,
    timeframe: session!.timeframe!,
    signal_ts: toUtcIso(signalBookmarkTs ?? currentBar!.ts),
    setup_id: values.setup_id || signalAnnotations.setup_id || "",
    side: values.side,
    entry: values.entry,
    sl: values.sl,
    tp: values.tp,
    notes: values.notes,
    calendar_flag: signalAnnotations.calendar_flag,
    calendar_tags: signalAnnotations.calendar_tags || undefined,
    confluence_tags:
      signalAnnotations.confluence.length > 0
        ? signalAnnotations.confluence.join(",")
        : undefined,
    at_key_level: signalAnnotations.at_key_level || undefined,
    level_type:
      signalAnnotations.level_type !== "none" ? signalAnnotations.level_type : undefined,
    consolidation_before: signalAnnotations.consolidation_before || undefined,
    blinded: session!.blinded ?? blinded,
    provenance: captureProvenance(signalBookmarkIdx ?? cursor),
    metadata: { confidence: signalAnnotations.confidence },
  });

  const handleStartTrade = form.handleSubmit(async (values) => {
    if (!session || !currentBar) return;
    setStarting(true);
    try {
      const store = useActiveTradeStore.getState();
      // Snapshot before the trade goes active: how far the operator had seen and
      // how long they deliberated is only meaningful as of the arming moment.
      const signalIdx = signalBookmarkIdx ?? cursor;
      const signalBar = getCandleAt({ candles, loadedStartOrdinal }, signalIdx) ??
        (signalBookmarkTs ? { ...currentBar, ts: signalBookmarkTs } : currentBar);
      store.setProvenance(captureProvenance(signalIdx));
      const tradeId = startTrade({
        signalIdx,
        signalTs: signalBar!.ts,
        setup_id: values.setup_id || signalAnnotations.setup_id || "",
        side: values.side,
        entry: values.entry,
        sl: values.sl,
        tp: values.tp,
        symbol: session.symbol!,
        signalId: activeSignalId,
        calendar_flag: signalAnnotations.calendar_flag,
        calendar_tags: signalAnnotations.calendar_tags,
      });

      const blob = await chartRef?.current?.takeScreenshot();
      if (blob && session.session_id) {
        const uploaded = await uploadScreenshot(session.session_id, "entry", blob, tradeId);
        store.setScreenshotPaths(uploaded.path, null);
        store.setEntryScreenshot(blob);
      }
    } finally {
      setStarting(false);
    }
  });

  const handleQuickSubmit = form.handleSubmit(async (values) => {
    if (!session || !currentBar) return;
    await submitTrade.mutateAsync(buildSubmitPayload(values));
    clearForm();
    onTradeSaved?.();
  });

  if (!session) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-zinc-400">Mark trade</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-zinc-500">Start a session to label trades.</CardContent>
      </Card>
    );
  }

  if (tradeStatus === "resolved") {
    return (
      // Keyed on the trade: its defaults snapshot the store at mount, so a new
      // trade has to mount a new form rather than reuse the last one's values.
      <TradeResolutionForm
        key={draftTradeId}
        session={session}
        onSaved={() => {
          clearForm();
          onTradeSaved?.();
        }}
      />
    );
  }

  const entry = toLevel(entryRaw);
  const sl = toLevel(slRaw);
  const tp = toLevel(tpRaw);
  const rr = entry != null && sl != null && tp != null ? riskReward(side, entry, sl, tp) : null;

  const tradeActive = tradeStatus === "active";

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-zinc-400">{tradeActive ? "Trade in progress" : "Mark trade"}</CardTitle>
        <p className="tnum font-mono text-xs text-zinc-500">
          {currentBar ? formatTs(currentBar.ts, blinded) : "No bar revealed yet"}
          {rr != null && <> · R:R {rr.toFixed(2)}</>}
        </p>
      </CardHeader>
      <CardContent>
        {/* Enter runs the primary action. Quick submit stays an explicit click. */}
        <form onSubmit={handleStartTrade} className="space-y-3">
          <ComboboxField
            control={form.control}
            name="setup_id"
            label="Setup"
            options={setupOptions}
            placeholder="Select setup"
            searchPlaceholder="Search patterns…"
            emptyText="No setup found."
            disabled={tradeActive}
          />

          <SelectField
            control={form.control}
            name="side"
            label="Side"
            options={SIDE_OPTIONS}
            // The schema wants the number 1 / -1, not the option string.
            parseValue={Number}
            disabled={tradeActive}
          />

          <div className="grid grid-cols-3 gap-2">
            {(["entry", "sl", "tp"] as const).map((name) => (
              <FormField
                key={name}
                control={form.control}
                name={name}
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>{LEVEL_LABELS[name]}</FormLabel>
                    <FormControl>
                      <Input
                        {...field}
                        className="tnum font-mono"
                        type="number"
                        step="any"
                        inputMode="decimal"
                        value={field.value ?? ""}
                        disabled={tradeActive}
                      />
                    </FormControl>
                    {!tradeActive && (
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => onArm?.(armed === name ? null : name)}
                        aria-pressed={armed === name}
                        title={`Pick ${LEVEL_LABELS[name]} from chart`}
                        className={cn(
                          "h-7 w-full gap-1 px-2 text-xs font-normal text-zinc-400",
                          armed === name && "ring-2 ring-[var(--color-ring)] text-zinc-50",
                        )}
                      >
                        <Crosshair className="h-3 w-3 shrink-0" aria-hidden="true" />
                        Chart
                      </Button>
                    )}
                    <FormMessage />
                  </FormItem>
                )}
              />
            ))}
          </div>
          {armed && !tradeActive && (
            <p className="text-xs text-operator">
              Click the chart to set {LEVEL_LABELS[armed]} · Esc to cancel
            </p>
          )}

          {!tradeActive && (
            <div className="flex flex-col gap-2">
              <Button type="submit" className="w-full" disabled={!currentBar || starting}>
                {starting ? "Starting…" : "Start trade"}
              </Button>
              <Button
                type="button"
                variant="outline"
                className="w-full"
                disabled={!currentBar || submitTrade.isPending}
                onClick={handleQuickSubmit}
              >
                {submitTrade.isPending ? "Submitting…" : "Quick submit"}
              </Button>

              <SkipRecordBlock
                session={session}
                blinded={blinded}
                fields={{
                  setup_id: setupId || signalAnnotations.setup_id || "",
                  side,
                  entry,
                  sl,
                  tp,
                  calendar_flag: signalAnnotations.calendar_flag,
                  calendar_tags: signalAnnotations.calendar_tags,
                  provenance: captureProvenance(signalBookmarkIdx ?? cursor),
                  signal_id: activeSignalId ?? undefined,
                }}
                onRecorded={() => {
                  clearForm();
                  useSignalStore.getState().reset();
                  onTradeSaved?.();
                }}
              />
            </div>
          )}

          {submitTrade.isError && (
            <p className="text-sm text-zinc-500">{submitTrade.error.message}</p>
          )}
          {submitTrade.isSuccess && submitTrade.data.outcome_kind !== "skipped" && (
            <p className="text-sm text-zinc-500">
              Trade saved — labeler returned {submitTrade.data.result}
            </p>
          )}
        </form>
      </CardContent>
    </Card>
  );
}
