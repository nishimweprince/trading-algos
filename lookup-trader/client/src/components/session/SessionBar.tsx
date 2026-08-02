import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { Eye, EyeOff, SlidersHorizontal } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DateRangePicker, type DateRange } from "@/components/ui/date-range-picker";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useSymbols, useTimeframes } from "@/hooks/useCandles";
import { useCreateSession } from "@/hooks/useSessions";
import { toDateKey, toIsoEnd, toIsoStart } from "@/lib/format";
import type { Session } from "@/types";

const TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];

const schema = z
  .object({
    symbol: z.string().min(1, "Pick a symbol"),
    timeframe: z.string().min(1, "Pick a timeframe"),
    range: z.object({
      from: z.date({ required_error: "Pick a start date" }),
      to: z.date({ required_error: "Pick an end date" }),
    }),
    blinded: z.boolean(),
  })
  .refine((v) => v.range.from <= v.range.to, {
    message: "The end date comes before the start date",
    path: ["range"],
  });

type FormValues = z.infer<typeof schema>;

interface SessionBarProps {
  onSessionStart: (session: Session, blinded: boolean) => void;
  session: Session | null;
  disabled?: boolean;
}

export function SessionBar({ onSessionStart, session, disabled }: SessionBarProps) {
  const { data: symbols = [] } = useSymbols();
  const createSession = useCreateSession();
  const [expanded, setExpanded] = useState(true);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      symbol: "EURUSD",
      timeframe: "H1",
      range: { from: new Date("2024-01-01T00:00:00"), to: new Date("2024-01-31T00:00:00") },
      blinded: false,
    },
  });

  const symbol = form.watch("symbol");
  const blinded = form.watch("blinded");
  const { data: timeframes = [] } = useTimeframes(symbol);
  const availableTimeframes = timeframes.length > 0 ? timeframes : TIMEFRAMES;
  const availableSymbols = symbols.length > 0 ? symbols : ["EURUSD"];

  const onSubmit = form.handleSubmit(async (values) => {
    const created = await createSession.mutateAsync({
      symbol: values.symbol,
      timeframe: values.timeframe,
      date_from: toIsoStart(toDateKey(values.range.from)),
      date_to: toIsoEnd(toDateKey(values.range.to)),
      blinded: values.blinded,
    });
    onSessionStart(created, values.blinded);
    setExpanded(false);
  });

  // Once a session is running the settings collapse to a summary line, so the chart
  // gets the vertical space back without the settings becoming unreachable.
  if (session && !expanded) {
    const range = form.getValues("range");
    return (
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-zinc-800 bg-zinc-950 px-4 py-2 text-sm">
        <span className="font-medium">{session.symbol}</span>
        <span className="rounded border border-zinc-800 px-1.5 py-0.5 text-xs text-zinc-400">
          {session.timeframe}
        </span>
        <span className="tnum font-mono text-xs text-zinc-400">
          {blinded ? "•••" : `${toDateKey(range.from)} → ${toDateKey(range.to)}`}
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
        <FormField
          control={form.control}
          name="symbol"
          render={({ field }) => (
            <FormItem className="w-32">
              <FormLabel className="micro-caps text-zinc-500">Symbol</FormLabel>
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
              <FormLabel className="micro-caps text-zinc-500">Timeframe</FormLabel>
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
          name="range"
          render={({ field }) => (
            <FormItem className="w-[17rem]">
              <FormLabel className="micro-caps text-zinc-500">Session window</FormLabel>
              <FormControl>
                <DateRangePicker
                  value={field.value as DateRange}
                  onChange={(range) => field.onChange(range ?? {})}
                  masked={blinded}
                  placeholder="Pick a date range"
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

        {createSession.isError && (
          <p className="w-full text-sm text-[var(--color-destructive)]">{createSession.error.message}</p>
        )}
      </form>
    </Form>
  );
}
