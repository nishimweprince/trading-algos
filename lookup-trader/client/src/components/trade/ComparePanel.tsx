import { useForm } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { StatCard } from "@/components/common/StatCard";
import { useCompare } from "@/hooks/useCompare";
import { useSetups } from "@/hooks/useSetups";
import { formatPercent } from "@/lib/format";
import type { Session } from "@/types";

const schema = z.object({
  setup_id: z.string().min(1, "Pick a setup"),
  trend_state: z.string().min(1),
  session: z.string().min(1),
  atr_bucket: z.string().min(1),
  rsi_band: z.string().min(1),
});

type FormValues = z.infer<typeof schema>;

const CONTEXT_FIELDS = [
  {
    name: "trend_state",
    label: "Trend",
    options: [
      { value: "up", label: "Up" },
      { value: "down", label: "Down" },
    ],
  },
  {
    name: "session",
    label: "Session",
    options: [
      { value: "asian", label: "Asian" },
      { value: "london", label: "London" },
      { value: "ny", label: "NY" },
    ],
  },
  {
    name: "atr_bucket",
    label: "Volatility",
    options: [
      { value: "low", label: "Low" },
      { value: "mid", label: "Mid" },
      { value: "high", label: "High" },
    ],
  },
  {
    name: "rsi_band",
    label: "RSI band",
    options: [
      { value: "oversold", label: "Oversold" },
      { value: "neutral", label: "Neutral" },
      { value: "overbought", label: "Overbought" },
    ],
  },
] as const;

interface ComparePanelProps {
  session: Session | null;
}

export function ComparePanel({ session }: ComparePanelProps) {
  const { data: setups = [] } = useSetups();
  const compare = useCompare();

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      setup_id: "",
      trend_state: "up",
      session: "london",
      atr_bucket: "mid",
      rsi_band: "neutral",
    },
  });

  const onSubmit = form.handleSubmit((values) => {
    if (!session?.symbol || !session.timeframe) return;
    compare.mutate({
      setup_id: values.setup_id,
      symbol: session.symbol,
      timeframe: session.timeframe,
      context: {
        trend_state: values.trend_state,
        session: values.session,
        atr_bucket: values.atr_bucket,
        rsi_band: values.rsi_band,
      },
    });
  });

  const result = compare.data;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-zinc-400">Compare</CardTitle>
        <p className="text-xs text-zinc-500">How this setup has resolved in a matching context.</p>
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

            <div className="grid grid-cols-2 gap-2">
              {CONTEXT_FIELDS.map(({ name, label, options }) => (
                <FormField
                  key={name}
                  control={form.control}
                  name={name}
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>{label}</FormLabel>
                      <Select value={field.value} onValueChange={field.onChange}>
                        <FormControl>
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                        </FormControl>
                        <SelectContent>
                          {options.map((o) => (
                            <SelectItem key={o.value} value={o.value}>
                              {o.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              ))}
            </div>

            <Button type="submit" className="w-full" disabled={!session || compare.isPending}>
              {compare.isPending ? "Comparing…" : "Run compare"}
            </Button>
            {compare.isError && (
              <p className="text-xs text-[var(--color-destructive)]">{compare.error.message}</p>
            )}
          </form>
        </Form>

        {result && (
          <div className="mt-3 grid grid-cols-2 gap-2">
            <StatCard
              title="Win rate"
              value={result.level_used === "no_signal" ? "No signal" : formatPercent(result.win_rate)}
              subtitle={`n=${result.decided} · ${result.level_used}`}
            />
            <StatCard
              title="Wilson CI"
              value={
                result.wilson_low != null
                  ? `${formatPercent(result.wilson_low)} – ${formatPercent(result.wilson_high)}`
                  : "—"
              }
            />
            <StatCard title="Expectancy" value={result.expectancy_r?.toFixed(2) ?? "—"} subtitle="in R" />
            <StatCard title="Wins" value={String(result.wins)} subtitle={`${result.timeouts} timeouts`} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
