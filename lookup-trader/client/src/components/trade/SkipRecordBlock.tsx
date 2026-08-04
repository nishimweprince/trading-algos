import { useForm } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { Button } from "@/components/ui/button";
import { Form } from "@/components/ui/form";
import { Label } from "@/components/ui/label";
import { SelectField } from "@/components/common/fields";
import { toOptions } from "@/lib/fieldOptions";
import { useReplayStore } from "@/hooks/useReplay";
import { useSubmitTrade } from "@/hooks/useTrades";
import { buildSkipPayload, resolveSkipSignalTs } from "@/lib/skipTrade";
import { SKIP_REASON_LABELS, SKIP_REASONS } from "@/lib/tradeLabels";
import type { Session, TradeProvenance } from "@/types";

const HELPER = "text-sm text-zinc-500";

const REASON_OPTIONS = toOptions(SKIP_REASONS, SKIP_REASON_LABELS);

const schema = z.object({
  skip_reason: z.string().min(1, "Pick why you passed"),
});

type FormValues = z.infer<typeof schema>;

export interface SkipRecordFields {
  setup_id: string;
  side: 1 | -1;
  entry?: number | null;
  sl?: number | null;
  tp?: number | null;
  notes?: string;
  calendar_flag?: boolean;
  calendar_tags?: string;
  provenance?: TradeProvenance | null;
  screenshot_entry?: string;
  /** When set, used directly instead of bookmark / cursor resolution. */
  signal_ts?: string;
}

interface SkipRecordBlockProps {
  session: Session;
  blinded?: boolean;
  fields: SkipRecordFields;
  onRecorded?: () => void;
  showBookmarkHint?: boolean;
}

export function SkipRecordBlock({
  session,
  blinded,
  fields,
  onRecorded,
  showBookmarkHint = true,
}: SkipRecordBlockProps) {
  const submitTrade = useSubmitTrade();
  const pause = useReplayStore((s) => s.pause);
  const scrub = useReplayStore((s) => s.scrub);
  const clearSignal = useReplayStore((s) => s.clearSignal);
  const signalBookmarkIdx = useReplayStore((s) => s.signalBookmarkIdx);
  const signalBookmarkTs = useReplayStore((s) => s.signalBookmarkTs);
  const currentBar = useReplayStore((s) => s.candles[s.cursor] ?? null);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { skip_reason: "" },
  });

  const handleRecord = form.handleSubmit(async ({ skip_reason }) => {
    if (!session.session_id || !fields.setup_id) return;

    const signal_ts =
      fields.signal_ts ??
      resolveSkipSignalTs(signalBookmarkTs, currentBar?.ts ?? null);
    if (!signal_ts) return;

    pause();
    if (signalBookmarkIdx != null && !fields.signal_ts) {
      scrub(signalBookmarkIdx);
    }

    await submitTrade.mutateAsync(
      buildSkipPayload({
        session_id: session.session_id,
        symbol: session.symbol!,
        timeframe: session.timeframe!,
        setup_id: fields.setup_id,
        side: fields.side,
        skip_reason,
        signal_ts,
        entry: fields.entry,
        sl: fields.sl,
        tp: fields.tp,
        notes: fields.notes,
        calendar_flag: fields.calendar_flag,
        calendar_tags: fields.calendar_tags,
        blinded: session.blinded ?? blinded,
        provenance: fields.provenance,
        screenshot_entry: fields.screenshot_entry,
      }),
    );

    clearSignal();
    form.reset();
    onRecorded?.();
  });

  return (
    // A <div>, not a <form>: this block renders inside TradeForm's and
    // TradeResolutionForm's own <form> elements, and nested forms are invalid
    // HTML. Hence the type="button" below driving handleSubmit by hand.
    <Form {...form}>
      <div className="space-y-2 border-t border-zinc-800 pt-3">
        <Label>Record skip</Label>
        {showBookmarkHint && (
          <p className={HELPER}>
            {signalBookmarkIdx != null
              ? `Signal bar ${signalBookmarkIdx + 1} marked — skip will attach there.`
              : "Mark the signal bar (S) so the skip stays on the setup, not the current cursor."}
          </p>
        )}
        <SelectField
          control={form.control}
          name="skip_reason"
          placeholder="Why did you pass?"
          options={REASON_OPTIONS}
        />
        <Button
          type="button"
          variant="outline"
          className="w-full"
          disabled={!fields.setup_id || submitTrade.isPending}
          onClick={handleRecord}
        >
          {submitTrade.isPending ? "Recording…" : "Record skip"}
        </Button>
        {submitTrade.isError && <p className={HELPER}>{submitTrade.error.message}</p>}
        {submitTrade.isSuccess && (
          <p className={HELPER}>Skip recorded — context stored, no trade scored.</p>
        )}
      </div>
    </Form>
  );
}
