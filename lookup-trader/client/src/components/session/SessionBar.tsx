import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { Eye, EyeOff, SlidersHorizontal } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DatePicker } from "@/components/ui/date-picker";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useSymbols, useCandleBounds, useTimeframes } from "@/hooks/useCandles";
import { useCreateSession } from "@/hooks/useSessions";
import { toDateKey, toIsoEnd, toIsoStart } from "@/lib/format";
import type { Session } from "@/types";
import moment from "moment";

const TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];

const schema = z
  .object({
    symbol: z.string().min(1, "Pick a symbol"),
    timeframe: z.string().min(1, "Pick a timeframe"),
    date_from: z.date({ required_error: "Pick a start date" }),
    date_to: z.date({ required_error: "Pick an end date" }),
    blinded: z.boolean(),
  })
  .refine((v) => v.date_from <= v.date_to, {
    message: "The end date comes before the start date",
    path: ["date_to"],
  });

type FormValues = z.infer<typeof schema>;

interface SessionBarProps {
  onSessionStart: (
    session: Session,
    blinded: boolean,
    range: { date_from: string; date_to: string },
  ) => void;
  onInstrumentChange?: (symbol: string, timeframe: string) => void;
  session: Session | null;
  disabled?: boolean;
}

function AppBranding({ compact = false }: { compact?: boolean }) {
  if (compact) {
    return (
      <span className="hidden text-xs text-zinc-600 sm:inline">Lookup Trader</span>
    );
  }
  return (
    <div className="mb-3 w-full border-b border-zinc-800/60 pb-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="text-sm font-medium">Lookup Trader</h1>
        <span className="text-xs text-zinc-400">Replay lab</span>
        <p className="text-xs text-zinc-600 sm:ml-auto">Bars past the cursor stay hidden</p>
      </div>
    </div>
  );
}

export function SessionBar({ onSessionStart, onInstrumentChange, session, disabled }: SessionBarProps) {
  const { data: symbols = [] } = useSymbols();
  const createSession = useCreateSession();
  const [expanded, setExpanded] = useState(true);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      symbol: "XAUUSD",
      timeframe: "H1",
      date_from: new Date("2024-01-01T00:00:00"),
      date_to: new Date(moment().subtract(1, 'day').toISOString()),
      blinded: false,
    },
  });

  const symbol = form.watch("symbol");
  const blinded = form.watch("blinded");
  const dateFrom = form.watch("date_from");
  const dateTo = form.watch("date_to");
  const timeframe = form.watch("timeframe");
  const { data: timeframes = [] } = useTimeframes(symbol);
  const { data: candleBounds } = useCandleBounds(symbol, timeframe);
  const availableTimeframes = timeframes.length > 0 ? timeframes : TIMEFRAMES;
  const availableSymbols = symbols.length > 0 ? symbols : ["EURUSD"];
  const dataRangeLabel =
    candleBounds?.min_ts && candleBounds?.max_ts
      ? `${toDateKey(new Date(candleBounds.min_ts))} → ${toDateKey(new Date(candleBounds.max_ts))}`
      : null;

  useEffect(() => {
    onInstrumentChange?.(symbol, timeframe);
  }, [symbol, timeframe, onInstrumentChange]);

  const onSubmit = form.handleSubmit(async (values) => {
    const date_from = toIsoStart(toDateKey(values.date_from));
    const date_to = toIsoEnd(toDateKey(values.date_to));
    const created = await createSession.mutateAsync({
      symbol: values.symbol,
      timeframe: values.timeframe,
      date_from,
      date_to,
      blinded: values.blinded,
    });
    onSessionStart(created, values.blinded, { date_from, date_to });
    setExpanded(false);
  });

  // Once a session is running the settings collapse to a summary line, so the chart
  // gets the vertical space back without the settings becoming unreachable.
  if (session && !expanded) {
    return (
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-zinc-800 bg-zinc-950 px-4 py-2 text-sm">
        <AppBranding compact />
        <span className="font-medium">{session.symbol}</span>
        <span className="rounded border border-zinc-800 px-1.5 py-0.5 text-xs text-zinc-400">
          {session.timeframe}
        </span>
        <span className="tnum font-mono text-xs text-zinc-400">
          {blinded ? "•••" : `${toDateKey(dateFrom)} → ${toDateKey(dateTo)}`}
        </span>
        <span className="flex items-center gap-1.5 text-xs text-zinc-500">
          {blinded ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
          {blinded ? "Blinded" : "Dates visible"}
        </span>
        <Button variant="ghost" size="sm" className="ml-auto gap-1.5" onClick={() => setExpanded(true)}>
          <SlidersHorizontal className="h-3.5 w-3.5" />
          Change session
        </Button>
      </div>
    );
  }

  return (
    <Form {...form}>
      <form
        onSubmit={onSubmit}
        className="flex flex-wrap items-start gap-x-4 gap-y-2 border-b border-zinc-800 bg-zinc-950 px-4 py-3"
      >
        <AppBranding />
        <FormField
          control={form.control}
          name="symbol"
          render={({ field }) => (
            <FormItem className="w-32">
              <FormLabel className="text-xs text-zinc-400">Symbol</FormLabel>
              <Select value={field.value} onValueChange={field.onChange}>
                <FormControl>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                </FormControl>
                <SelectContent>
                  {availableSymbols.map((s) => (
                    <SelectItem key={s} value={s}>
                      {s}
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
          name="timeframe"
          render={({ field }) => (
            <FormItem className="w-24">
              <FormLabel className="text-xs text-zinc-400">Timeframe</FormLabel>
              <Select value={field.value} onValueChange={field.onChange}>
                <FormControl>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                </FormControl>
                <SelectContent>
                  {availableTimeframes.map((tf) => (
                    <SelectItem key={tf} value={tf}>
                      {tf}
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
          name="date_from"
          render={({ field }) => (
            <FormItem className="w-40">
              <FormLabel className="text-xs text-zinc-400">Date from</FormLabel>
              <FormControl>
                <DatePicker
                  value={field.value}
                  onChange={field.onChange}
                  masked={blinded}
                  // Keeps the two ends in order without waiting for a validation error.
                  disabledDays={{ after: dateTo ?? new Date() }}
                  placeholder="Start date"
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />

        <FormField
          control={form.control}
          name="date_to"
          render={({ field }) => (
            <FormItem className="w-40">
              <FormLabel className="text-xs text-zinc-400">Date to</FormLabel>
              <FormControl>
                <DatePicker
                  value={field.value}
                  onChange={field.onChange}
                  masked={blinded}
                  disabledDays={[{ after: new Date() }, ...(dateFrom ? [{ before: dateFrom }] : [])]}
                  placeholder="End date"
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />

        <FormField
          control={form.control}
          name="blinded"
          render={({ field }) => (
            <FormItem className="pt-[1.375rem]">
              <div className="flex h-9 items-center gap-2">
                <FormControl>
                  <Switch id="blinded" checked={field.value} onCheckedChange={field.onChange} />
                </FormControl>
                <Label htmlFor="blinded" className="cursor-pointer">
                  Blinded
                </Label>
              </div>
            </FormItem>
          )}
        />

        <div className="flex items-center gap-2 pt-[1.375rem]">
          <Button type="submit" disabled={disabled || createSession.isPending}>
            {createSession.isPending ? "Starting…" : session ? "Restart session" : "Start session"}
          </Button>
          {session && (
            <Button type="button" variant="ghost" onClick={() => setExpanded(false)}>
              Cancel
            </Button>
          )}
        </div>

        {dataRangeLabel && (
          <p className="w-full text-xs text-zinc-500">Data available: {dataRangeLabel}</p>
        )}

        {createSession.isError && (
          <p className="w-full text-sm text-[var(--color-destructive)]">{createSession.error.message}</p>
        )}
      </form>
    </Form>
  );
}
