import { useForm } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { SkipRecordBlock } from "@/components/trade/SkipRecordBlock";
import { Form, FormControl, FormField, FormItem, FormLabel } from "@/components/ui/form";
import { Slider } from "@/components/ui/slider";
import {
  ChipToggleField,
  InputField,
  SelectField,
  SwitchField,
  TextareaField,
} from "@/components/common/fields";
import { toOptions, type FieldOption } from "@/lib/fieldOptions";
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
import type { Session, TradeMetadata } from "@/types";

const schema = z.object({
  notes: z.string().optional(),
  calendar_flag: z.boolean().optional(),
  calendar_tags: z.string().optional(),
  observed_result: z.string().optional(),
  observed_trend: z.string().optional(),
  // Named for the trading session (asian/london/ny), not the labelling session
  // in the `session` prop — the two used to collide inside this component.
  trading_session: z.string().min(1),
  confluence: z.array(z.string()),
  market_structure: z.string().optional(),
  htf_alignment: z.string().optional(),
  entry_quality: z.string().optional(),
  confidence: z.number().min(1).max(5),
});

type FormValues = z.infer<typeof schema>;

const SESSION_OPTIONS: FieldOption[] = TRADING_SESSIONS.map((s) => ({
  value: s,
  label: formatTradingSession(s),
}));
const CONFLUENCE_OPTIONS = toOptions(CONFLUENCE_TAGS, CONFLUENCE_LABELS);
const TREND_OPTIONS = toOptions(OBSERVED_TRENDS, OBSERVED_TREND_LABELS);
const STRUCTURE_OPTIONS = toOptions(MARKET_STRUCTURES, MARKET_STRUCTURE_LABELS);
const HTF_OPTIONS = toOptions(HTF_ALIGNMENTS, HTF_ALIGNMENT_LABELS);
const QUALITY_OPTIONS = toOptions(ENTRY_QUALITIES, ENTRY_QUALITY_LABELS);
const RESULT_OPTIONS = toOptions(OBSERVED_RESULTS, OBSERVED_RESULT_LABELS);

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
  const setup_id = useActiveTradeStore((s) => s.setup_id);
  const side = useActiveTradeStore((s) => s.side);
  const entry = useActiveTradeStore((s) => s.entry);
  const sl = useActiveTradeStore((s) => s.sl);
  const tp = useActiveTradeStore((s) => s.tp);
  const signalTs = useActiveTradeStore((s) => s.signalTs);
  const provenance = useActiveTradeStore((s) => s.provenance);
  const entryScreenshotPath = useActiveTradeStore((s) => s.entryScreenshotPath);

  // These snapshot the store at mount. That is correct because the parent keys
  // this component on draftTradeId, so a new trade mounts a fresh form.
  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      notes: "",
      calendar_flag,
      calendar_tags,
      observed_result: liveResult ?? "",
      observed_trend: "",
      trading_session: tradingSession,
      confluence: [],
      market_structure: "",
      htf_alignment: "",
      entry_quality: "",
      confidence: 3,
    },
  });

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
      session: values.trading_session,
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
            <SelectField
              control={form.control}
              name="trading_session"
              label="Trading session"
              options={SESSION_OPTIONS}
            />

            <SelectField
              control={form.control}
              name="observed_trend"
              label="Trend (your read)"
              placeholder="Optional"
              options={TREND_OPTIONS}
            />

            <ChipToggleField
              control={form.control}
              name="confluence"
              label="Confluence"
              options={CONFLUENCE_OPTIONS}
            />

            <div className="grid grid-cols-2 gap-2">
              <SelectField
                control={form.control}
                name="market_structure"
                label="Structure"
                placeholder="Optional"
                options={STRUCTURE_OPTIONS}
              />
              <SelectField
                control={form.control}
                name="htf_alignment"
                label="HTF alignment"
                placeholder="Optional"
                options={HTF_OPTIONS}
              />
            </div>

            <SelectField
              control={form.control}
              name="entry_quality"
              label="Entry quality"
              placeholder="Optional"
              options={QUALITY_OPTIONS}
            />

            {/* Slider has no wrapper: it is the only one of its kind. */}
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

            <SwitchField
              control={form.control}
              name="calendar_flag"
              label="High-impact news day"
            />

            <InputField
              control={form.control}
              name="calendar_tags"
              label="Calendar tags"
              placeholder="NFP, FOMC"
            />

            <TextareaField
              control={form.control}
              name="notes"
              label="Notes"
              placeholder="What made this a setup?"
            />

            <SelectField
              control={form.control}
              name="observed_result"
              label="Your read (stored, never scored)"
              placeholder="Optional"
              options={RESULT_OPTIONS}
            />

            <Button type="submit" className="w-full" disabled={submitTrade.isPending}>
              {submitTrade.isPending ? "Saving…" : "Save occurrence"}
            </Button>
            {submitTrade.isError && (
              <p className="text-sm text-zinc-500">{submitTrade.error.message}</p>
            )}

            <SkipRecordBlock
              session={session}
              showBookmarkHint={false}
              fields={{
                setup_id,
                side,
                entry,
                sl,
                tp,
                calendar_flag,
                calendar_tags,
                provenance,
                screenshot_entry: entryScreenshotPath ?? undefined,
                signal_ts: signalTs,
              }}
              onRecorded={() => {
                useActiveTradeStore.getState().reset();
                onSaved();
              }}
            />
          </form>
        </Form>
      </CardContent>
    </Card>
  );
}
