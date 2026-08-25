import { faDownload } from "@fortawesome/free-solid-svg-icons";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  BaselineSeries,
  createChart,
  LineSeries,
  LineStyle,
  type BaselineData,
  type LineData,
  type Time,
} from "lightweight-charts";
import { Button } from "@/components/ui/button";
import { buildEquityChartData, type EquityChartDatum } from "@/lib/equity";
import { formatUnit, formatWhen } from "@/lib/format";
import { Icon } from "@/lib/icon";
import { useTheme } from "@/lib/theme";
import type { EquityCurvePoint, PerformanceUnit } from "@/lib/types";

interface EquityDrawdownChartProps {
  points: EquityCurvePoint[];
  unit: PerformanceUnit;
  onDownloadSettings: () => void;
}

const LOSS = "#ef4444";

export function EquityDrawdownChart({
  points,
  unit,
  onDownloadSettings,
}: EquityDrawdownChartProps) {
  const host = useRef<HTMLDivElement>(null);
  const data = useMemo(() => buildEquityChartData(points), [points]);
  const [cursor, setCursor] = useState<EquityChartDatum | null>(data.at(-1) ?? null);
  const { theme } = useTheme();
  const dark = theme !== "light";

  useEffect(() => {
    setCursor(data.at(-1) ?? null);
  }, [data]);

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const bg = dark ? "#000000" : "#ffffff";
    const fg = dark ? "#ffffff" : "#000000";
    const grid = dark ? "#222222" : "#e5e5e5";
    const priceFormatter = (value: number) => formatAxisValue(value, unit);
    const chart = createChart(el, {
      layout: {
        background: { color: bg },
        textColor: dark ? "#8a8a8a" : "#6b6b6b",
        fontFamily: "DM Sans, DM Sans Fallback, ui-sans-serif, system-ui, sans-serif",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: grid },
        horzLines: { color: grid },
      },
      width: el.clientWidth,
      height: 340,
      timeScale: { borderColor: grid, timeVisible: true, secondsVisible: false },
      rightPriceScale: {
        borderColor: grid,
        scaleMargins: { top: 0.12, bottom: 0.12 },
      },
      crosshair: {
        vertLine: { color: fg, labelBackgroundColor: fg },
        horzLine: { color: fg, labelBackgroundColor: fg },
      },
      localization: { priceFormatter },
    });
    const drawdownSeries = chart.addSeries(BaselineSeries, {
      baseValue: { type: "price", price: 0 },
      topFillColor1: "rgba(239, 68, 68, 0)",
      topFillColor2: "rgba(239, 68, 68, 0)",
      topLineColor: "rgba(239, 68, 68, 0)",
      bottomFillColor1: "rgba(239, 68, 68, 0.08)",
      bottomFillColor2: "rgba(239, 68, 68, 0.32)",
      bottomLineColor: LOSS,
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      priceFormat: { type: "custom", formatter: priceFormatter },
    });
    const equitySeries = chart.addSeries(LineSeries, {
      color: fg,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      priceFormat: { type: "custom", formatter: priceFormatter },
    });
    const equityData: LineData<Time>[] = data.map((point) => ({
      time: point.time as Time,
      value: point.equity,
    }));
    const drawdownData: BaselineData<Time>[] = data.map((point) => ({
      time: point.time as Time,
      value: point.drawdown,
    }));
    drawdownSeries.setData(drawdownData);
    equitySeries.setData(equityData);
    drawdownSeries.createPriceLine({
      price: 0,
      color: grid,
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      axisLabelVisible: false,
      title: "",
    });
    const byTime = new Map(data.map((point) => [point.time, point]));
    chart.subscribeCrosshairMove((event) => {
      if (typeof event.time !== "number") {
        setCursor(data.at(-1) ?? null);
        return;
      }
      setCursor(byTime.get(event.time) ?? data.at(-1) ?? null);
    });
    if (data.length > 0) chart.timeScale().fitContent();

    const resize = () => chart.applyOptions({ width: el.clientWidth });
    const observer = new ResizeObserver(resize);
    observer.observe(el);
    return () => {
      observer.disconnect();
      chart.remove();
    };
  }, [dark, data, unit]);

  return (
    <section className="border-b border-border" aria-labelledby="equity-drawdown-title">
      <header className="flex flex-col gap-4 border-b border-border px-5 py-4 md:flex-row md:items-center md:justify-between md:px-10">
        <div>
          <h2 id="equity-drawdown-title" className="text-sm font-medium">
            Equity &amp; drawdown
          </h2>
          <div className="mt-1.5 flex items-center gap-4 text-[11px] uppercase text-muted-foreground">
            <span className="inline-flex items-center gap-1.5">
              <span className="h-px w-4 bg-foreground" aria-hidden="true" />
              Net equity
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span className="h-px w-4 bg-loss" aria-hidden="true" />
              Drawdown
            </span>
          </div>
        </div>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <div className="min-w-0 text-[11px] sm:text-right">
            <p className="truncate uppercase text-muted-foreground">
              {cursor ? formatWhen(cursor.ts) : "No marked equity points"}
            </p>
            <p className="mt-1 flex gap-3 sm:justify-end">
              <span>{cursor ? formatUnit(cursor.equity, unit) : "—"}</span>
              <span className="text-loss">
                DD {cursor ? formatUnit(cursor.drawdown, unit) : "—"}
              </span>
            </p>
          </div>
          <Button type="button" variant="outline" size="sm" onClick={onDownloadSettings}>
            <Icon icon={faDownload} className="h-3 w-3" />
            Download settings
          </Button>
        </div>
      </header>
      <div className="relative min-h-[340px] bg-background">
        {data.length === 0 ? (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center text-xs text-muted-foreground">
            This run has no marked equity points.
          </div>
        ) : null}
        <div ref={host} className="h-[340px] w-full" />
      </div>
    </section>
  );
}

function formatAxisValue(value: number, unit: PerformanceUnit): string {
  const sign = value < 0 ? "−" : "";
  const amount = Math.abs(value).toLocaleString("en-US", { maximumFractionDigits: 1 });
  return unit === "dollars" ? `${sign}$${amount}` : `${sign}${amount}p`;
}
