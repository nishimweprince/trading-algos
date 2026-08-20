import { useEffect, useMemo, useState } from "react";
import { format } from "date-fns";
import { FormProvider, useForm } from "react-hook-form";
import {
  faChartLine,
  faDownload,
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
import { downloadBacktestCsv } from "@/lib/csv";
import { dayEndUtc, dayStartUtc } from "@/lib/format";
import { Icon } from "@/lib/icon";
import { filterBySession } from "@/lib/stats";
import { type BacktestReport, type BacktestRequest, type Candle } from "@/lib/types";

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
  const performanceUnit = form.watch("performanceUnit");
  const [sessionFilter, setSessionFilter] = useState<string | null>(null);
  const [report, setReport] = useState<BacktestReport | null>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [dollarsAvailable, setDollarsAvailable] = useState(false);

  useEffect(() => {
    void fetchConfig()
      .then((config) => {
        form.reset({
          ...form.getValues(),
          symbol: config.symbol,
          sessions: config.sessions.length > 0 ? config.sessions : form.getValues("sessions"),
          entryMode: config.entry_mode,
          hedgeRatioInitial: config.hedge_ratio_initial,
          hedgeFailureK: config.hedge_failure_k,
          hedgeRatioStaged: config.hedge_ratio_staged,
          ocoBufferMode: config.oco_buffer_mode,
          ocoBufferValue: config.oco_buffer_value,
          ocoExpiryBars: config.oco_expiry_bars,
          allowReentry: config.allow_reentry,
          lockPips: config.lock_pips,
          stopMode: config.stop_mode,
          slMult: config.sl_mult,
          fixedStopPips: config.fixed_stop_pips,
          rr: config.rr,
          minStopPips: config.min_stop_pips,
          qty: config.qty,
          orbMinutes: config.orb_minutes,
          entryDelayMinutes: config.entry_delay_minutes,
          anchorToleranceMinutes: config.anchor_tolerance_minutes,
          performanceUnit: config.performance_unit,
        });
        setDollarsAvailable(config.dollars_per_pip_per_qty !== null);
      })
      .catch(() => {
        /* Keep built-in defaults when the API is down. */
      });
  }, [form]);

  const visiblePairs = useMemo(
    () => (report ? filterBySession(report.trade_pairs, sessionFilter) : []),
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

  function handleDownloadCsv() {
    if (!report || visiblePairs.length === 0) return;
    downloadBacktestCsv(report, visiblePairs, sessionFilter);
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
              {report
                ? [
                    `BAR_TIMEFRAME=${report.timeframe}`,
                    `ENTRY_MODE=${report.entry_mode}`,
                    `ORB_MINUTES=${report.orb_minutes}`,
                    `ENTRY_DELAY_MINUTES=${report.entry_delay_minutes}`,
                    `ANCHOR_TOLERANCE_MINUTES=${report.anchor_tolerance_minutes}`,
                    report.stop_mode === "fixed_pips"
                      ? `STOP_MODE=fixed_pips(${report.fixed_stop_pips})`
                      : "STOP_MODE=bar_range",
                  ].join(" · ")
                : `${symbol} · ${timeframe} · Tokyo / London / New York`}
            </p>
            {report ? (
              <p className="mt-1 text-[11px] uppercase text-muted-foreground">
                {report.session_anchor_stats
                  .map((row) => {
                    const p50 = row.anchor_drift_p50 == null ? "—" : `${row.anchor_drift_p50.toFixed(0)}m`;
                    return `${row.session} drift p50=${p50}`;
                  })
                  .join(" · ")}
              </p>
            ) : null}
            <div className="mt-4 flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between">
              <div className="max-w-2xl">
                <h1 className="text-xl font-medium">Lock the survivor.</h1>
                <p className="mt-3 max-w-lg text-xs text-muted-foreground">
                  Dual entry at each cash-session open. Stop is twice the first bar. When one side is stopped, the other locks.
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

          <KpiStrip report={report} unit={performanceUnit} />
          <SessionRail
            active={sessionFilter}
            present={report ? [...new Set(report.trade_pairs.map((pair) => pair.session))] : sessions}
            pairs={report?.trade_pairs ?? []}
            unit={performanceUnit}
            anchorStats={report?.session_anchor_stats}
            onSelect={setSessionFilter}
          />

          <section id="run" className="grid scroll-mt-4 border-b border-border lg:grid-cols-[280px_minmax(0,1fr)]">
            <div className="border-b border-border p-5 lg:border-b-0 lg:border-r lg:p-6">
              <p className="mb-4 text-[11px] uppercase text-muted-foreground">Parameters</p>
              <RunForm
                loading={loading}
                dollarsAvailable={dollarsAvailable}
                onValid={(values) => void onValid(values)}
              />
            </div>
            <div id="chart" className="scroll-mt-4 min-w-0">
              <BacktestChart candles={candles} events={report?.events ?? []} session={sessionFilter} />
            </div>
          </section>

          <section id="blotter" className="scroll-mt-4 px-5 py-6 md:px-10">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h2 className="text-sm font-medium">Blotter</h2>
              <div className="flex items-center gap-3">
                <span className="text-[11px] uppercase text-muted-foreground">
                  {visiblePairs.length} pairs
                </span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={!report || visiblePairs.length === 0}
                  onClick={handleDownloadCsv}
                >
                  <Icon icon={faDownload} className="h-3 w-3" />
                  Download CSV
                </Button>
              </div>
            </div>
            <TradeBlotter
              pairs={visiblePairs}
              unit={performanceUnit}
              context={
                report
                  ? {
                      symbol: report.symbol,
                      timeframe: report.timeframe,
                      source: report.source,
                      performance_unit: report.performance_unit,
                    }
                  : null
              }
            />
          </section>
        </main>
      </div>
    </FormProvider>
  );
}

function toRequest(form: RunFormState): BacktestRequest {
  return {
    symbol: form.symbol,
    timeframe: form.timeframe,
    date_from: form.dateFrom ? dayStartUtc(format(form.dateFrom, "yyyy-MM-dd")) : null,
    date_to: form.dateTo ? dayEndUtc(format(form.dateTo, "yyyy-MM-dd")) : null,
    source: form.source === "auto" ? null : form.source,
    entry_mode: form.entryMode,
    hedge_ratio_initial: form.entryMode === "contingent_hedge" ? form.hedgeRatioInitial : null,
    hedge_trigger_mode: form.entryMode === "contingent_hedge" ? "failure_zone" : null,
    hedge_failure_k: form.entryMode === "contingent_hedge" ? form.hedgeFailureK : null,
    hedge_ratio_staged: form.entryMode === "contingent_hedge" ? form.hedgeRatioStaged : null,
    oco_buffer_mode: form.entryMode === "oco_bracket" ? form.ocoBufferMode : null,
    oco_buffer_value: form.entryMode === "oco_bracket" ? form.ocoBufferValue : null,
    oco_expiry_bars: form.entryMode === "oco_bracket" ? form.ocoExpiryBars : null,
    allow_reentry: form.entryMode === "oco_bracket" ? form.allowReentry : null,
    lock_pips: form.lockPips,
    stop_mode: form.stopMode,
    sl_mult: form.slMult,
    fixed_stop_pips: form.stopMode === "fixed_pips" ? form.fixedStopPips : null,
    rr: form.rr,
    min_stop_pips: form.minStopPips,
    qty: form.qty,
    sessions: form.sessions,
    performance_unit: form.performanceUnit,
    orb_minutes: form.orbMinutes,
    entry_delay_minutes: form.entryDelayMinutes,
    anchor_tolerance_minutes: form.anchorToleranceMinutes,
  };
}
