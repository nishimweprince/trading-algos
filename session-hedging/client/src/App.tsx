import { useEffect, useMemo, useState } from "react";
import { format } from "date-fns";
import { FormProvider, useForm } from "react-hook-form";
import {
  faChartLine,
  faPlay,
  faSliders,
  faTable,
} from "@fortawesome/free-solid-svg-icons";
import { BacktestChart } from "@/components/BacktestChart";
import { KpiStrip } from "@/components/KpiStrip";
import { DEFAULT_FORM, RunForm, type RunFormState } from "@/components/RunForm";
import { SessionRail } from "@/components/SessionRail";
import { ThemeToggle } from "@/components/ThemeToggle";
import { TradeBlotter } from "@/components/TradeBlotter";
import { Button } from "@/components/ui/button";
import { ApiError, fetchCandles, fetchConfig, runBacktest } from "@/lib/api";
import { dayEndUtc, dayStartUtc } from "@/lib/format";
import { Icon } from "@/lib/icon";
import { filterBySession } from "@/lib/stats";
import { TIMEFRAMES, type BacktestReport, type BacktestRequest, type Candle, type Timeframe } from "@/lib/types";

const NAV = [
  { href: "#run", label: "Run", icon: faSliders },
  { href: "#chart", label: "Chart", icon: faChartLine },
  { href: "#blotter", label: "Blotter", icon: faTable },
] as const;

export default function App() {
  const form = useForm<RunFormState>({
    defaultValues: DEFAULT_FORM,
    mode: "onSubmit",
  });
  const symbol = form.watch("symbol");
  const timeframe = form.watch("timeframe");
  const sessions = form.watch("sessions");
  const [sessionFilter, setSessionFilter] = useState<string | null>(null);
  const [report, setReport] = useState<BacktestReport | null>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    void fetchConfig()
      .then((config) => {
        form.reset({
          ...form.getValues(),
          symbol: config.symbol,
          timeframe: isTimeframe(config.timeframe) ? config.timeframe : form.getValues("timeframe"),
          sessions: config.sessions.length > 0 ? config.sessions : form.getValues("sessions"),
          lockPips: config.lock_pips,
          slMult: config.sl_mult,
          rr: config.rr,
          minStopPips: config.min_stop_pips,
          qty: config.qty,
        });
      })
      .catch(() => {
        /* Keep built-in defaults when the API is down. */
      });
  }, [form]);

  const visibleTrades = useMemo(
    () => (report ? filterBySession(report.trades, sessionFilter) : []),
    [report, sessionFilter],
  );

  async function onValid(values: RunFormState) {
    setLoading(true);
    setError(null);
    try {
      const body = toRequest(values);
      const next = await runBacktest(body);
      setReport(next);
      setSessionFilter(null);
      const bars = await fetchCandles({
        symbol: next.symbol,
        timeframe: next.timeframe,
        count: Math.max(next.bar_count, 1),
        to: body.date_to,
        source: body.source,
      });
      const fromMs = body.date_from ? Date.parse(body.date_from) : null;
      setCandles(
        fromMs ? bars.candles.filter((candle) => Date.parse(candle.ts) >= fromMs) : bars.candles,
      );
    } catch (err) {
      setReport(null);
      setCandles([]);
      setError(err instanceof ApiError ? err.message : "Backtest failed");
    } finally {
      setLoading(false);
    }
  }

  function handleClear() {
    setReport(null);
    setCandles([]);
    setError(null);
    setSessionFilter(null);
  }

  return (
    <FormProvider {...form}>
      <div className="flex min-h-screen bg-background text-foreground">
        <aside className="sticky top-0 hidden h-screen w-[200px] shrink-0 flex-col border-r border-border md:flex">
          <a href="#run" className="px-6 pt-7 text-sm font-medium">
            SH.
          </a>
          <nav className="mt-12 flex flex-col gap-3 px-6 text-xs">
            {NAV.map((item) => (
              <a
                key={item.href}
                href={item.href}
                className="inline-flex items-center gap-2 text-foreground/80 hover:text-foreground"
              >
                <Icon icon={item.icon} className="h-3 w-3 opacity-60" />
                {item.label}
              </a>
            ))}
          </nav>
          <div className="mt-auto px-6 pb-7">
            <ThemeToggle />
          </div>
        </aside>

        <main className="min-w-0 flex-1">
          <header className="flex items-center justify-between border-b border-border px-5 py-3 md:hidden">
            <span className="text-sm font-medium">SH.</span>
            <ThemeToggle />
          </header>
          <nav className="flex gap-4 border-b border-border px-5 py-2.5 text-xs md:hidden">
            {NAV.map((item) => (
              <a key={item.href} href={item.href} className="inline-flex items-center gap-2 text-muted-foreground">
                <Icon icon={item.icon} className="h-3 w-3" />
                {item.label}
              </a>
            ))}
          </nav>

          <section className="border-b border-border px-5 py-8 md:px-10 md:py-10">
            <p className="text-[11px] uppercase text-muted-foreground">
              {symbol} · {timeframe} · Tokyo / London / New York
            </p>
            <div className="mt-4 flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between">
              <div className="max-w-2xl">
                <h1 className="text-xl font-medium">Lock the survivor.</h1>
                <p className="mt-3 max-w-lg text-xs text-muted-foreground">
                  Dual entry at each cash-session open. Stop is twice the first bar. When one side is
                  stopped, the other locks.
                </p>
              </div>
              <div className="flex shrink-0 flex-col gap-2 sm:flex-row lg:flex-col xl:flex-row">
                <Button type="button" onClick={() => void form.handleSubmit(onValid)()} disabled={loading}>
                  <Icon icon={faPlay} className="h-3 w-3" />
                  {loading ? "Running…" : "Run backtest"}
                </Button>
                <Button type="button" variant="pill" onClick={handleClear}>
                  Clear results
                </Button>
              </div>
            </div>
          </section>

          {error ? (
            <div className="border-b border-border px-5 py-2.5 text-xs text-loss md:px-10">{error}</div>
          ) : null}

          <KpiStrip report={report} />
          <SessionRail
            active={sessionFilter}
            present={report ? [...new Set(report.trades.map((trade) => trade.session))] : sessions}
            trades={report?.trades ?? []}
            onSelect={setSessionFilter}
          />

          <section id="run" className="grid scroll-mt-4 border-b border-border lg:grid-cols-[280px_minmax(0,1fr)]">
            <div className="border-b border-border p-5 lg:border-b-0 lg:border-r lg:p-6">
              <p className="mb-4 text-[11px] uppercase text-muted-foreground">Parameters</p>
              <RunForm loading={loading} onValid={(values) => void onValid(values)} />
            </div>
            <div id="chart" className="scroll-mt-4 min-w-0">
              <BacktestChart candles={candles} events={report?.events ?? []} session={sessionFilter} />
            </div>
          </section>

          <section id="blotter" className="scroll-mt-4 px-5 py-6 md:px-10">
            <div className="mb-3 flex items-end justify-between">
              <h2 className="text-sm font-medium">Blotter</h2>
              <span className="text-[11px] uppercase text-muted-foreground">
                {visibleTrades.length} legs
              </span>
            </div>
            <TradeBlotter trades={visibleTrades} />
          </section>
        </main>
      </div>
    </FormProvider>
  );
}

function isTimeframe(value: string): value is Timeframe {
  return (TIMEFRAMES as readonly string[]).includes(value);
}

function toRequest(form: RunFormState): BacktestRequest {
  return {
    symbol: form.symbol,
    timeframe: form.timeframe,
    date_from: form.dateFrom ? dayStartUtc(format(form.dateFrom, "yyyy-MM-dd")) : null,
    date_to: form.dateTo ? dayEndUtc(format(form.dateTo, "yyyy-MM-dd")) : null,
    source: form.source === "auto" ? null : form.source,
    lock_pips: form.lockPips,
    sl_mult: form.slMult,
    rr: form.rr,
    min_stop_pips: form.minStopPips,
    qty: form.qty,
    sessions: form.sessions,
  };
}
