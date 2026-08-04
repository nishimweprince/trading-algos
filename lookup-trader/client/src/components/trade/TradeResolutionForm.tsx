import { useForm } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useSubmitTrade } from "@/hooks/useTrades";
import {
  CONFLUENCE_LABELS,
  CONFLUENCE_TAGS,
  ENTRY_QUALITIES,
  ENTRY_QUALITY_LABELS,
  HTF_ALIGNMENTS,
  HTF_ALIGNMENT_LABELS,
  MARKET_STRUCTURES,
  MARKET_STRUCTURE_LABELS,
  OBSERVED_RESULT_LABELS,
  OBSERVED_RESULTS,
  OBSERVED_TRENDS,
  OBSERVED_TREND_LABELS,
} from "@/lib/tradeLabels";
import { formatTradingSession, TRADING_SESSIONS } from "@/lib/tradingSession";
import { toUtcIso } from "@/lib/format";
import { useActiveTradeStore } from "@/stores/activeTradeStore";
import { cn } from "@/lib/utils";
import type { Session, TradeMetadata } from "@/types";

const schema = z.object({
  notes: z.string().optional(),
  calendar_flag: z.boolean().optional(),
  calendar_tags: z.string().optional(),
  observed_result: z.string().optional(),
  observed_trend: z.string().optional(),
  session: z.string().min(1),
  confluence: z.array(z.string()),
  market_structure: z.string().optional(),
  htf_alignment: z.string().optional(),
  entry_quality: z.string().optional(),
  confidence: z.number().min(1).max(5),
});

type FormValues = z.infer<typeof schema>;

interface TradeResolutionFormProps {
  session: Session;
  onSaved: () => void;
}

export function TradeResolutionForm({ session, onSaved }: TradeResolutionFormProps) {
  const submitTrade = useSubmitTrade();
  const liveResult = useActiveTradeStore((s) => s.liveResult);
  const pips = useActiveTradeStore((s) => s.pips);
  const tradingSession = useActiveTradeStore((s) => s.tradingSession);
  const calendar_flag = useActiveTradeStore((s) => s.calendar_flag);
  const calendar_tags = useActiveTradeStore((s) => s.calendar_tags);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      notes: "",
      calendar_flag,
      calendar_tags,
      observed_result: liveResult ?? "",
      observed_trend: "",
      session: tradingSession,
      confluence: [],
      market_structure: "",
      htf_alignment: "",
      entry_quality: "",
      confidence: 3,
    },
  });

  const confluence = form.watch("confluence");

  const toggleConfluence = (tag: string) => {
    const current = form.getValues("confluence");
    if (current.includes(tag)) {
      form.setValue(
        "confluence",
        current.filter((t) => t !== tag),
        { shouldDirty: true },
      );
    } else {
      form.setValue("confluence", [...current, tag], { shouldDirty: true });
    }
  };

  const onSubmit = form.handleSubmit(async (values) => {
    const trade = useActiveTradeStore.getState();
    const metadata: TradeMetadata = {};
    if (values.market_structure) metadata.market_structure = values.market_structure;
    if (values.htf_alignment) metadata.htf_alignment = values.htf_alignment;
    if (values.entry_quality) metadata.entry_quality = values.entry_quality;
    metadata.confidence = values.confidence;

    await submitTrade.mutateAsync({
      session_id: session.session_id,
      symbol: session.symbol!,
      timeframe: session.timeframe!,
      signal_ts: toUtcIso(trade.signalTs),
      setup_id: trade.setup_id,
      side: trade.side,
      entry: trade.entry,
      sl: trade.sl,
      tp: trade.tp,
      notes: values.notes,
      calendar_flag: values.calendar_flag,
      calendar_tags: values.calendar_tags,
      observed_result: values.observed_result || undefined,
      observed_trend: values.observed_trend || undefined,
      session: values.session,
      pips_captured: trade.pips,
      screenshot_entry: trade.entryScreenshotPath ?? undefined,
      screenshot_exit: trade.exitScreenshotPath ?? undefined,
      confluence_tags: values.confluence.length > 0 ? values.confluence.join(",") : undefined,
      metadata: Object.keys(metadata).length > 0 ? metadata : undefined,
      blinded: session.blinded,
      // Captured when the trade was armed, not now — by this point the operator
      // has necessarily seen the bars that resolved it.
      provenance: trade.provenance ?? undefined,
    });

    trade.reset();
    onSaved();
  });

  return (
    <Card className="border-emerald-900/40">
      <CardHeader>
        <CardTitle className="text-emerald-400">Label trade</CardTitle>
        <p className="text-xs text-zinc-500">
          Resolved {liveResult} · {pips >= 0 ? "+" : ""}
          {pips.toFixed(1)} pips · {formatTradingSession(tradingSession)} session
        </p>
      </CardHeader>
      <CardContent>
        <Form {...form}>
          <form onSubmit={onSubmit} className="space-y-3">
            <FormField
              control={form.control}
              name="session"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Trading session</FormLabel>
                  <Select value={field.value} onValueChange={field.onChange}>
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {TRADING_SESSIONS.map((s) => (
                        <SelectItem key={s} value={s}>
                          {formatTradingSession(s)}
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
              name="observed_trend"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Trend (your read)</FormLabel>
                  <Select value={field.value} onValueChange={field.onChange}>
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue placeholder="Optional" />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {OBSERVED_TRENDS.map((t) => (
                        <SelectItem key={t} value={t}>
                          {OBSERVED_TREND_LABELS[t]}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <FormMessage />
                </FormItem>
              )}
            />

            {/* Plain markup, not FormItem/FormLabel: those read the FormField
                context, and the chips are driven by setValue rather than a field. */}
            <div className="space-y-1.5">
              <Label>Confluence</Label>
              <div className="flex flex-wrap gap-1.5">
                {CONFLUENCE_TAGS.map((tag) => (
                  <button
                    key={tag}
                    type="button"
                    onClick={() => toggleConfluence(tag)}
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
            </div>

            <div className="grid grid-cols-2 gap-2">
              <FormField
                control={form.control}
                name="market_structure"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Structure</FormLabel>
                    <Select value={field.value} onValueChange={field.onChange}>
                      <FormControl>
                        <SelectTrigger>
                          <SelectValue placeholder="Optional" />
                        </SelectTrigger>
                      </FormControl>
                      <SelectContent>
                        {MARKET_STRUCTURES.map((s) => (
                          <SelectItem key={s} value={s}>
                            {MARKET_STRUCTURE_LABELS[s]}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="htf_alignment"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>HTF alignment</FormLabel>
                    <Select value={field.value} onValueChange={field.onChange}>
                      <FormControl>
                        <SelectTrigger>
                          <SelectValue placeholder="Optional" />
                        </SelectTrigger>
                      </FormControl>
                      <SelectContent>
                        {HTF_ALIGNMENTS.map((s) => (
                          <SelectItem key={s} value={s}>
                            {HTF_ALIGNMENT_LABELS[s]}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </FormItem>
                )}
              />
            </div>

            <FormField
              control={form.control}
              name="entry_quality"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Entry quality</FormLabel>
                  <Select value={field.value} onValueChange={field.onChange}>
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue placeholder="Optional" />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {ENTRY_QUALITIES.map((s) => (
                        <SelectItem key={s} value={s}>
                          {ENTRY_QUALITY_LABELS[s]}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </FormItem>
              )}
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

            <FormField
              control={form.control}
              name="calendar_flag"
              render={({ field }) => (
                <FormItem>
                  <div className="flex items-center gap-2">
                    <FormControl>
                      <Switch id="cal-res" checked={!!field.value} onCheckedChange={field.onChange} />
                    </FormControl>
                    <Label htmlFor="cal-res" className="cursor-pointer">
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

            <FormField
              control={form.control}
              name="observed_result"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Your read (stored, never scored)</FormLabel>
                  <Select value={field.value} onValueChange={field.onChange}>
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue placeholder="Optional" />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {OBSERVED_RESULTS.map((r) => (
                        <SelectItem key={r} value={r}>
                          {OBSERVED_RESULT_LABELS[r]}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </FormItem>
              )}
            />

            <Button type="submit" className="w-full" disabled={submitTrade.isPending}>
              {submitTrade.isPending ? "Saving…" : "Save occurrence"}
            </Button>
            {submitTrade.isError && (
              <p className="text-xs text-[var(--color-destructive)]">{submitTrade.error.message}</p>
            )}
          </form>
        </Form>
      </CardContent>
    </Card>
  );
}
