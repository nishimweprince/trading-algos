import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { useSetups } from "@/hooks/useSetups";
import { useSubmitTrade } from "@/hooks/useTrades";
import { useCurrentBar } from "@/hooks/useReplay";
import { formatTs } from "@/lib/format";
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
  dateFrom?: string;
  dateTo?: string;
}

/** "" or a non-numeric entry means "no line on the chart" rather than NaN. */
function toLevel(raw: unknown): number | null {
  if (raw === "" || raw == null) return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

export function TradeForm({ session, blinded, levels, onLevelsChange, dateFrom, dateTo }: TradeFormProps) {
  const currentBar = useCurrentBar();
  const { data: setups = [] } = useSetups();
  const submitTrade = useSubmitTrade();

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

  // The three price fields drive the chart's price lines. Subscribing once keeps
  // the form the single source of truth instead of writing values in two places.
  useEffect(() => {
    const subscription = form.watch((values, { name }) => {
      if (name !== "entry" && name !== "sl" && name !== "tp") return;
      onLevelsChange({
        entry: toLevel(values.entry),
        sl: toLevel(values.sl),
        tp: toLevel(values.tp),
      });
    });
    return () => subscription.unsubscribe();
  }, [form, onLevelsChange]);

  const onSubmit = form.handleSubmit(async (values) => {
    if (!session || !currentBar) return;
    await submitTrade.mutateAsync({
      session_id: session.session_id,
      symbol: session.symbol!,
      timeframe: session.timeframe!,
      signal_ts: currentBar.ts,
      setup_id: values.setup_id,
      side: values.side,
      entry: values.entry,
      sl: values.sl,
      tp: values.tp,
      notes: values.notes,
      calendar_flag: values.calendar_flag,
      calendar_tags: values.calendar_tags,
      observed_result: values.observed_result,
      date_from: dateFrom,
      date_to: dateTo,
    });
    form.reset({ ...form.getValues(), entry: "", sl: "", tp: "", notes: "", observed_result: "" });
    onLevelsChange({ entry: null, sl: null, tp: null });
  });

  if (!session) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="micro-caps text-zinc-400">Mark trade</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-zinc-500">Start a session to label trades.</CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="micro-caps text-zinc-400">Mark trade</CardTitle>
        <p className="tnum font-mono text-xs text-zinc-500">
          {currentBar ? formatTs(currentBar.ts, blinded) : "No bar revealed yet"}
        </p>
      </CardHeader>
      <CardContent>
        <Form {...form}>
          <form onSubmit={onSubmit} className="space-y-3">
            <FormField
              control={form.control}
              name="setup_id"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Setup</FormLabel>
                  <Select value={field.value} onValueChange={field.onChange}>
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue placeholder="Select setup" />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {setups.map((s) => (
                        <SelectItem key={s.setup_id} value={s.setup_id}>
                          {s.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
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
                    onValueChange={(v) => field.onChange(Number(v) as 1 | -1)}
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
              {(
                [
                  { name: "entry", label: "Entry" },
                  { name: "sl", label: "Stop" },
                  { name: "tp", label: "Target" },
                ] as const
              ).map(({ name, label }) => (
                <FormField
                  key={name}
                  control={form.control}
                  name={name}
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>{label}</FormLabel>
                      <FormControl>
                        <Input
                          {...field}
                          className="tnum font-mono"
                          type="number"
                          step="any"
                          inputMode="decimal"
                          value={field.value ?? ""}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              ))}
            </div>

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
                  <FormMessage />
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
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="observed_result"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Your read (optional)</FormLabel>
                  <FormControl>
                    <Input {...field} placeholder="win / loss — stored, never scored" />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <Button type="submit" className="w-full" disabled={!currentBar || submitTrade.isPending}>
              {submitTrade.isPending ? "Submitting…" : "Submit trade"}
            </Button>
            {submitTrade.isError && (
              <p className="text-xs text-[var(--color-destructive)]">{submitTrade.error.message}</p>
            )}
            {submitTrade.isSuccess && (
              <p className="text-xs text-emerald-400">
                Trade saved — labeler returned {submitTrade.data.result}
              </p>
            )}
          </form>
        </Form>
      </CardContent>
    </Card>
  );
}
