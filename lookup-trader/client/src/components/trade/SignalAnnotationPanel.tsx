import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Form } from "@/components/ui/form";
import { Slider } from "@/components/ui/slider";
import {
  ChipToggleField,
  ComboboxField,
  InputField,
  SelectField,
  SwitchField,
} from "@/components/common/fields";
import { StatCard } from "@/components/common/StatCard";
import { toOptions } from "@/lib/fieldOptions";
import { useSignalContext } from "@/hooks/useCandles";
import { useSubmitSignal } from "@/hooks/useSignals";
import { useSetupOptions } from "@/hooks/useSetups";
import { captureProvenance, useReplayStore } from "@/hooks/useReplay";
import { formatPercent, toUtcIso } from "@/lib/format";
import { api } from "@/lib/api";
import { CONFLUENCE_LABELS, CONFLUENCE_TAGS } from "@/lib/tradeLabels";
import { useSignalStore } from "@/stores/signalStore";
import type { CompareContext, Session } from "@/types";
import { FormControl, FormField, FormItem, FormLabel } from "@/components/ui/form";

const LEVEL_TYPES = ["none", "prior_swing_high", "prior_swing_low", "round_number"] as const;
const LEVEL_TYPE_LABELS: Record<(typeof LEVEL_TYPES)[number], string> = {
  none: "None",
  prior_swing_high: "Prior swing high",
  prior_swing_low: "Prior swing low",
  round_number: "Round number",
};

const schema = z.object({
  setup_id: z.string().optional(),
  side: z.string().optional(),
  confluence: z.array(z.string()),
  calendar_flag: z.boolean(),
  calendar_tags: z.string().optional(),
  confidence: z.number().min(1).max(5),
  at_key_level: z.boolean(),
  level_type: z.string(),
  consolidation_before: z.boolean(),
});

type FormValues = z.infer<typeof schema>;

const CONFLUENCE_OPTIONS = toOptions(CONFLUENCE_TAGS, CONFLUENCE_LABELS);
const LEVEL_OPTIONS = toOptions(LEVEL_TYPES, LEVEL_TYPE_LABELS);
const SIDE_OPTIONS = [
  { value: "", label: "Not set" },
  { value: "1", label: "Long" },
  { value: "-1", label: "Short" },
];

interface SignalAnnotationPanelProps {
  session: Session | null;
  blinded?: boolean;
}

export function SignalAnnotationPanel({ session, blinded }: SignalAnnotationPanelProps) {
  const signalBookmarkIdx = useReplayStore((s) => s.signalBookmarkIdx);
  const signalBookmarkTs = useReplayStore((s) => s.signalBookmarkTs);
  const setupOptions = useSetupOptions();
  const submitSignal = useSubmitSignal();
  const activeSignalId = useSignalStore((s) => s.activeSignalId);
  const lastSignal = useSignalStore((s) => s.lastSignal);
  const setActiveSignalId = useSignalStore((s) => s.setActiveSignalId);
  const setLastSignal = useSignalStore((s) => s.setLastSignal);
  const setAnnotations = useSignalStore((s) => s.setAnnotations);
  const storedAnnotations = useSignalStore((s) => s.annotations);

  const signalTs = signalBookmarkTs;
  const { data: signalContext } = useSignalContext(
    session?.symbol ?? "",
    session?.timeframe ?? "",
    signalTs,
  );

  const { data: calendarFlags } = useQuery({
    queryKey: ["calendar-flags", session?.symbol, signalTs],
    queryFn: () => api.getCalendarFlags(session!.symbol!, toUtcIso(signalTs!)),
    enabled: !!session?.symbol && !!signalTs,
    staleTime: Infinity,
    retry: false,
  });

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      setup_id: storedAnnotations.setup_id ?? "",
      side: storedAnnotations.side != null ? String(storedAnnotations.side) : "",
      confluence: storedAnnotations.confluence,
      calendar_flag: storedAnnotations.calendar_flag,
      calendar_tags: storedAnnotations.calendar_tags,
      confidence: storedAnnotations.confidence,
      at_key_level: storedAnnotations.at_key_level,
      level_type: storedAnnotations.level_type,
      consolidation_before: storedAnnotations.consolidation_before,
    },
  });

  useEffect(() => {
    if (calendarFlags?.high_impact_today) {
      form.setValue("calendar_flag", true, { shouldDirty: true });
    }
  }, [calendarFlags?.high_impact_today, form]);

  useEffect(() => {
    const sub = form.watch((values) => {
      setAnnotations({
        setup_id: values.setup_id || undefined,
        side: values.side === "1" ? 1 : values.side === "-1" ? -1 : undefined,
        confluence: values.confluence ?? [],
        calendar_flag: values.calendar_flag ?? false,
        calendar_tags: values.calendar_tags ?? "",
        confidence: values.confidence ?? 3,
        at_key_level: values.at_key_level ?? false,
        level_type: values.level_type ?? "none",
        consolidation_before: values.consolidation_before ?? false,
      });
    });
    return () => sub.unsubscribe();
  }, [form, setAnnotations]);

  if (!session || signalBookmarkIdx == null || !signalTs) return null;

  const onSubmit = form.handleSubmit(async (values) => {
    const provenance = captureProvenance(signalBookmarkIdx);
    const compareContext: CompareContext = {};
    if (signalContext) {
      for (const key of [
        "trend_state",
        "session",
        "atr_bucket",
        "rsi_band",
        "day_of_week",
        "htf_trend_state",
        "ema_slope_bucket",
        "atr_change_bucket",
      ] as const) {
        const v = signalContext[key];
        if (v != null) compareContext[key] = v as never;
      }
    }
    if (values.side === "1" || values.side === "-1") {
      compareContext.side = Number(values.side) as 1 | -1;
    }
    if (values.calendar_flag) compareContext.calendar_flag = true;
    if (values.at_key_level) compareContext.at_key_level = true;
    if (values.level_type && values.level_type !== "none") {
      compareContext.level_type = values.level_type;
    }
    if (values.consolidation_before) compareContext.consolidation_before = true;
    if (values.confluence.length > 0) compareContext.confluence_tags = values.confluence;

    const signal = await submitSignal.mutateAsync({
      session_id: session.session_id,
      symbol: session.symbol!,
      timeframe: session.timeframe!,
      signal_ts: toUtcIso(signalTs),
      setup_id: values.setup_id || undefined,
      side: values.side === "1" ? 1 : values.side === "-1" ? -1 : undefined,
      cursor_idx: signalBookmarkIdx,
      bars_visible: signalBookmarkIdx + 1,
      peeked: provenance.peeked,
      blinded: session.blinded ?? blinded,
      compare_context: values.setup_id ? compareContext : undefined,
      compare_min_samples: Number(import.meta.env.VITE_COMPARE_MIN_SAMPLES) || 3,
      annotations: {
        confluence_tags:
          values.confluence.length > 0 ? values.confluence.join(",") : undefined,
        calendar_flag: values.calendar_flag,
        calendar_tags: values.calendar_tags,
        confidence: values.confidence,
        at_key_level: values.at_key_level || undefined,
        level_type:
          values.level_type !== "none"
            ? (values.level_type as "prior_swing_high" | "prior_swing_low" | "round_number" | "none")
            : undefined,
        consolidation_before: values.consolidation_before || undefined,
        annotation_sources: {
          setup_id: "operator",
          confluence: "operator",
          calendar_flag: "operator",
          confidence: "operator",
        },
      },
    });

    setActiveSignalId(signal.id);
    setLastSignal(signal);
  });

  const compare = lastSignal?.compare_at_signal;

  return (
    <Card className="border-operator/30">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm text-operator">Signal bar {signalBookmarkIdx + 1}</CardTitle>
        <p className="text-xs text-zinc-500">
          Annotate at candle close before marking levels. Snapshot freezes context and compare.
        </p>
      </CardHeader>
      <CardContent>
        <Form {...form}>
          <form onSubmit={onSubmit} className="space-y-3">
            <ComboboxField
              control={form.control}
              name="setup_id"
              label="Setup (optional)"
              options={setupOptions}
              placeholder="Select setup"
              searchPlaceholder="Search patterns…"
              emptyText="No setup found."
            />

            <SelectField
              control={form.control}
              name="side"
              label="Side intent"
              options={SIDE_OPTIONS}
            />

            <ChipToggleField
              control={form.control}
              name="confluence"
              label="Confluence"
              options={CONFLUENCE_OPTIONS}
            />

            <SwitchField control={form.control} name="at_key_level" label="At key level" />

            <SelectField
              control={form.control}
              name="level_type"
              label="Level type"
              options={LEVEL_OPTIONS}
            />

            <SwitchField
              control={form.control}
              name="consolidation_before"
              label="Consolidation before signal"
            />

            <FormField
              control={form.control}
              name="confidence"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Confidence ({field.value})</FormLabel>
                  <FormControl>
                    <Slider
                      min={1}
                      max={5}
                      step={1}
                      value={[field.value]}
                      onValueChange={([v]) => field.onChange(v)}
                    />
                  </FormControl>
                </FormItem>
              )}
            />

            <SwitchField control={form.control} name="calendar_flag" label="High-impact news day" />
            {calendarFlags?.high_impact_today && (
              <p className="text-xs text-zinc-500">
                Auto-flagged — high-impact {calendarFlags.events.map((e) => e.title).join(", ")} near
                this bar.
              </p>
            )}

            <InputField
              control={form.control}
              name="calendar_tags"
              label="Calendar tags"
              placeholder="NFP, FOMC"
            />

            <Button type="submit" className="w-full" disabled={submitSignal.isPending}>
              {submitSignal.isPending
                ? "Recording…"
                : activeSignalId
                  ? "Update signal snapshot"
                  : "Record signal snapshot"}
            </Button>

            {submitSignal.isError && (
              <p className="text-sm text-zinc-500">{submitSignal.error.message}</p>
            )}

            {activeSignalId && (
              <p className="text-xs text-zinc-500">Signal linked · id {activeSignalId.slice(0, 8)}…</p>
            )}

            {compare && (
              <div className="grid grid-cols-2 gap-2 pt-1">
                <StatCard
                  title="Compare at signal"
                  value={
                    compare.level_used === "no_signal"
                      ? "No signal"
                      : formatPercent(compare.win_rate)
                  }
                  subtitle={`n=${compare.decided} · ${compare.level_used}`}
                />
                <StatCard
                  title="Expectancy"
                  value={compare.expectancy_r?.toFixed(2) ?? "—"}
                  subtitle="in R at signal time"
                />
              </div>
            )}

            {signalContext?.context_reliable === false && (
              <p className="text-xs text-zinc-500">
                Only {signalContext.warmup_bars_available} warmup bars — context unreliable.
              </p>
            )}
          </form>
        </Form>
      </CardContent>
    </Card>
  );
}
