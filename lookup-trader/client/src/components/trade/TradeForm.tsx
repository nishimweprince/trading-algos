import { useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { Crosshair } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { LEVEL_LABELS, inferSide, type LevelPick, type PriceLevelKey } from "@/components/chart/PriceLines";
import type { ReplayChartHandle } from "@/components/chart/ReplayChart";
import { TradeResolutionForm } from "@/components/trade/TradeResolutionForm";
import { SkipRecordBlock } from "@/components/trade/SkipRecordBlock";
import { captureProvenance, useReplayStore, useCurrentBar } from "@/hooks/useReplay";
import { useSetupOptions, useSetups } from "@/hooks/useSetups";
import { useSubmitTrade } from "@/hooks/useTrades";
import { formatTs, toUtcIso } from "@/lib/format";
import { riskReward } from "@/lib/pips";
import { uploadScreenshot } from "@/lib/screenshot";
import { useActiveTradeStore } from "@/stores/activeTradeStore";
import { cn } from "@/lib/utils";
import type { Session } from "@/types";

/** Price fields arrive from the inputs as strings; the API takes numbers. */
const price = (label: string) =>
  z
    .string()
    .min(1, `${label} is required`)
    .refine((v) => Number(v) > 0, `${label} must be a price above zero`)
    .transform(Number);

const schema = z.object({
  setup_id: z.string().min(1, "Pick a setup"),
  side: z.union([z.literal(1), z.literal(-1)]),
  entry: price("Entry"),
  sl: price("Stop"),
  tp: price("Target"),
  notes: z.string().optional(),
  calendar_flag: z.boolean().optional(),
  calendar_tags: z.string().optional(),
  observed_result: z.string().optional(),
});

type FormValues = z.input<typeof schema>;
type ParsedValues = z.output<typeof schema>;

interface TradeFormProps {
  session: Session | null;
  blinded?: boolean;
  levels: { entry: number | null; sl: number | null; tp: number | null };
  onLevelsChange: (levels: { entry: number | null; sl: number | null; tp: number | null }) => void;
  armed?: PriceLevelKey | null;
  onArm?: (field: PriceLevelKey | null) => void;
  pick?: LevelPick | null;
  chartRef?: React.RefObject<ReplayChartHandle | null>;
  onTradeSaved?: () => void;
}

function toLevel(raw: unknown): number | null {
  if (raw === "" || raw == null) return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

export function TradeForm({
  session,
  blinded,
  levels,
  onLevelsChange,
  armed = null,
  onArm,
  pick,
  chartRef,
  onTradeSaved,
}: TradeFormProps) {
  const currentBar = useCurrentBar();
  const cursor = useReplayStore((s) => s.cursor);
  const signalBookmarkIdx = useReplayStore((s) => s.signalBookmarkIdx);
  const setupOptions = useSetupOptions();
  const { data: setups = [] } = useSetups();
  const submitTrade = useSubmitTrade();
  const tradeStatus = useActiveTradeStore((s) => s.status);
  const startTrade = useActiveTradeStore((s) => s.startTrade);
  const noteLevelRevision = useActiveTradeStore((s) => s.noteLevelRevision);
  const [starting, setStarting] = useState(false);
  const sideTouched = useRef(false);

  const form = useForm<FormValues, unknown, ParsedValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      setup_id: "",
      side: 1,
      entry: levels.entry != null ? String(levels.entry) : "",
      sl: levels.sl != null ? String(levels.sl) : "",
      tp: levels.tp != null ? String(levels.tp) : "",
      notes: "",
      calendar_flag: false,
      calendar_tags: "",
      observed_result: "",
    },
  });

  useEffect(() => {
    const subscription = form.watch((values, { name }) => {
      if (name !== "entry" && name !== "sl" && name !== "tp") return;
      noteLevelRevision();
      onLevelsChange({
        entry: toLevel(values.entry),
        sl: toLevel(values.sl),
        tp: toLevel(values.tp),
      });
      const entry = toLevel(values.entry);
      const tp = toLevel(values.tp);
      if (!sideTouched.current && entry != null && tp != null) {
        form.setValue("side", inferSide(entry, tp), { shouldDirty: true });
      }
    });
    return () => subscription.unsubscribe();
  }, [form, onLevelsChange, noteLevelRevision]);

  useEffect(() => {
    if (!pick) return;
    form.setValue(pick.field, String(pick.price), { shouldValidate: true, shouldDirty: true });
  }, [pick, form]);

  const setupId = form.watch("setup_id");
  useEffect(() => {
    if (sideTouched.current) return;
    const setup = setups.find((s) => s.setup_id === setupId);
    if (setup?.default_side === 1 || setup?.default_side === -1) {
      form.setValue("side", setup.default_side, { shouldDirty: true });
    }
  }, [setupId, setups, form]);

  const clearForm = () => {
    form.reset({
      ...form.getValues(),
      entry: "",
      sl: "",
      tp: "",
      notes: "",
      observed_result: "",
    });
    onLevelsChange({ entry: null, sl: null, tp: null });
    sideTouched.current = false;
  };

  const buildSubmitPayload = (values: ParsedValues) => ({
    session_id: session!.session_id,
    symbol: session!.symbol!,
    timeframe: session!.timeframe!,
    signal_ts: toUtcIso(currentBar!.ts),
    setup_id: values.setup_id,
    side: values.side,
    entry: values.entry,
    sl: values.sl,
    tp: values.tp,
    notes: values.notes,
    calendar_flag: values.calendar_flag,
    calendar_tags: values.calendar_tags,
    observed_result: values.observed_result || undefined,
    blinded: session!.blinded ?? blinded,
    provenance: captureProvenance(cursor),
  });

  const handleStartTrade = form.handleSubmit(async (values) => {
    if (!session || !currentBar) return;
    setStarting(true);
    try {
      const store = useActiveTradeStore.getState();
      // Snapshot before the trade goes active: how far the operator had seen and
      // how long they deliberated is only meaningful as of the arming moment.
      store.setProvenance(captureProvenance(cursor));
      const tradeId = startTrade({
        signalIdx: cursor,
        signalTs: currentBar.ts,
        setup_id: values.setup_id,
        side: values.side,
        entry: values.entry,
        sl: values.sl,
        tp: values.tp,
        symbol: session.symbol!,
        calendar_flag: values.calendar_flag,
        calendar_tags: values.calendar_tags,
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
      <TradeResolutionForm
        session={session}
        onSaved={() => {
          clearForm();
          onTradeSaved?.();
        }}
      />
    );
  }

  const entry = toLevel(form.watch("entry"));
  const sl = toLevel(form.watch("sl"));
  const tp = toLevel(form.watch("tp"));
  const side = form.watch("side");
  const rr =
    entry != null && sl != null && tp != null ? riskReward(side, entry, sl, tp) : null;

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
        <Form {...form}>
          <form className="space-y-3">
            <FormField
              control={form.control}
              name="setup_id"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Setup</FormLabel>
                  <FormControl>
                    <Combobox
                      options={setupOptions}
                      value={field.value}
                      onChange={field.onChange}
                      placeholder="Select setup"
                      searchPlaceholder="Search patterns…"
                      emptyText="No setup found."
                      disabled={tradeActive}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="side"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Side</FormLabel>
                  <Select
                    value={String(field.value)}
                    onValueChange={(v) => {
                      sideTouched.current = true;
                      field.onChange(Number(v) as 1 | -1);
                    }}
                    disabled={tradeActive}
                  >
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      <SelectItem value="1">Long</SelectItem>
                      <SelectItem value="-1">Short</SelectItem>
                    </SelectContent>
                  </Select>
                  <FormMessage />
                </FormItem>
              )}
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
              <p className="text-xs text-[#38bdf8]">
                Click the chart to set {LEVEL_LABELS[armed]} · Esc to cancel
              </p>
            )}

            {!tradeActive && (
              <>
                <FormField
                  control={form.control}
                  name="calendar_flag"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex items-center gap-2">
                        <FormControl>
                          <Switch id="calendar" checked={!!field.value} onCheckedChange={field.onChange} />
                        </FormControl>
                        <Label htmlFor="calendar" className="cursor-pointer">
                          High-impact news day
                        </Label>
                      </div>
                    </FormItem>
                  )}
                />

                <FormField
                  control={form.control}
                  name="calendar_tags"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Calendar tags</FormLabel>
                      <FormControl>
                        <Input {...field} placeholder="NFP, FOMC" />
                      </FormControl>
                    </FormItem>
                  )}
                />

                <FormField
                  control={form.control}
                  name="notes"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Notes</FormLabel>
                      <FormControl>
                        <Textarea {...field} placeholder="What made this a setup?" />
                      </FormControl>
                    </FormItem>
                  )}
                />
              </>
            )}

            {!tradeActive && (
              <div className="flex flex-col gap-2">
                <Button
                  type="button"
                  className="w-full"
                  disabled={!currentBar || starting}
                  onClick={handleStartTrade}
                >
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
                    setup_id: form.watch("setup_id"),
                    side: form.watch("side"),
                    entry: toLevel(form.watch("entry")),
                    sl: toLevel(form.watch("sl")),
                    tp: toLevel(form.watch("tp")),
                    notes: form.watch("notes"),
                    calendar_flag: form.watch("calendar_flag"),
                    calendar_tags: form.watch("calendar_tags"),
                    provenance: captureProvenance(signalBookmarkIdx ?? cursor),
                  }}
                  onRecorded={() => {
                    clearForm();
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
        </Form>
      </CardContent>
    </Card>
  );
}
