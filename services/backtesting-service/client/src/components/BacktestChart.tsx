import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type Time,
} from "lightweight-charts";
import { useTheme } from "@/lib/theme";
import { markerEvents } from "@/lib/stats";
import { SESSION_COLOR, type Candle, type EngineEvent } from "@/lib/types";

interface BacktestChartProps {
  candles: Candle[];
  events: EngineEvent[];
  session: string | null;
}

const UP = "#22c55e";
const DOWN = "#ef4444";

function toChartTime(ts: string): Time {
  return Math.floor(new Date(ts).getTime() / 1000) as Time;
}

export function BacktestChart({ candles, events, session }: BacktestChartProps) {
  const host = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const { theme } = useTheme();
  const dark = theme !== "light";

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const bg = dark ? "#000000" : "#ffffff";
    const fg = dark ? "#ffffff" : "#000000";
    const grid = dark ? "#222222" : "#e5e5e5";
    const chart = createChart(el, {
      layout: {
        background: { color: bg },
        textColor: fg,
        fontFamily: "DM Sans, DM Sans Fallback, ui-sans-serif, system-ui, sans-serif",
      },
      grid: {
        vertLines: { color: grid },
        horzLines: { color: grid },
      },
      width: el.clientWidth,
      height: 420,
      timeScale: { borderColor: grid, timeVisible: true },
      rightPriceScale: { borderColor: grid },
      crosshair: { vertLine: { color: fg }, horzLine: { color: fg } },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: UP,
      downColor: DOWN,
      borderVisible: false,
      wickUpColor: UP,
      wickDownColor: DOWN,
    });
    chartRef.current = chart;
    seriesRef.current = series;
    markersRef.current = createSeriesMarkers(series, []);
    const resize = () => chart.applyOptions({ width: el.clientWidth });
    const observer = new ResizeObserver(resize);
    observer.observe(el);
    return () => {
      observer.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      markersRef.current = null;
    };
  }, [dark]);

  useEffect(() => {
    const series = seriesRef.current;
    const chart = chartRef.current;
    if (!series || !chart) return;
    const data: CandlestickData<Time>[] = candles.map((candle) => ({
      time: toChartTime(candle.ts),
      open: candle.open,
      high: candle.high,
      low: candle.low,
      close: candle.close,
    }));
    series.setData(data);
    const markers = markerEvents(events, session).map((event) => {
      const sessionColor = SESSION_COLOR[event.session] ?? "#ffffff";
      if (event.kind === "entry") {
        return {
          time: toChartTime(event.ts),
          position: "belowBar" as const,
          color: dark ? sessionColor : "#000000",
          shape: "arrowUp" as const,
          text: event.session.replaceAll("_", " "),
        };
      }
      const bucket = String(event.detail.bucket ?? "");
      const color = bucket === "win" ? UP : bucket === "loss" ? DOWN : dark ? "#ffffff" : "#000000";
      return {
        time: toChartTime(event.ts),
        position: "aboveBar" as const,
        color,
        shape: "circle" as const,
        text: String(event.detail.side ?? "exit"),
      };
    });
    markersRef.current?.setMarkers(markers);
    if (data.length > 0) chart.timeScale().fitContent();
  }, [candles, events, session, dark]);

  return (
    <div className="relative h-full min-h-[420px] bg-background">
      {candles.length === 0 ? (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center text-xs text-muted-foreground">
          Run a backtest to draw the session opens.
        </div>
      ) : null}
      <div ref={host} className="h-[420px] w-full" />
    </div>
  );
}
