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
import type { Candle } from "@/types";

interface ReplayChartProps {
  candles: Candle[];
  blinded?: boolean;
  entry?: number | null;
  sl?: number | null;
  tp?: number | null;
}

/** Bars of empty space kept to the right of the newest candle. */
const RIGHT_OFFSET_BARS = 12;

const OPERATOR = "#38bdf8";
const UP = "#22c55e";
const DOWN = "#ef4444";

function toChartTime(ts: string): Time {
  return Math.floor(new Date(ts).getTime() / 1000) as Time;
}

function toBar(c: Candle): CandlestickData<Time> {
  return { time: toChartTime(c.ts), open: c.open, high: c.high, low: c.low, close: c.close };
}

export function ReplayChart({ candles, blinded = false, entry, sl, tp }: ReplayChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const priceLinesRef = useRef<ReturnType<ISeriesApi<"Candlestick">["createPriceLine"]>[]>([]);
  const prevRef = useRef<{ count: number; firstTime: Time | null }>({ count: 0, firstTime: null });

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: { background: { color: "#09090b" }, textColor: "#a1a1aa" },
      grid: { vertLines: { color: "#27272a" }, horzLines: { color: "#27272a" } },
      rightPriceScale: { borderColor: "#27272a", scaleMargins: { top: 0.1, bottom: 0.1 } },
      timeScale: {
        borderColor: "#27272a",
        timeVisible: !blinded,
        secondsVisible: false,
        // Keep permanent empty space to the right so the operator can always see
        // where the next bar will land instead of reading a chart pinned to its edge.
        rightOffset: RIGHT_OFFSET_BARS,
        barSpacing: 8,
        rightBarStaysOnScroll: true,
        shiftVisibleRangeOnNewBar: true,
      },
      crosshair: { mode: 1 },
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
    prevRef.current = { count: 0, firstTime: null };

    const ro = new ResizeObserver(() => {
      if (containerRef.current) {
        chart.applyOptions({
          width: containerRef.current.clientWidth,
          height: containerRef.current.clientHeight,
        });
      }
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      markersRef.current = null;
      priceLinesRef.current = [];
    };
  }, [blinded]);

  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;

    const firstTime = candles.length > 0 ? toChartTime(candles[0].ts) : null;
    const prev = prevRef.current;
    const isAppend =
      candles.length === prev.count + 1 && prev.count > 0 && firstTime === prev.firstTime;

    if (isAppend) {
      // One new revealed bar: append it and leave the operator's scroll position alone.
      series.update(toBar(candles[candles.length - 1]));
    } else {
      series.setData(candles.map(toBar));
      chartRef.current?.timeScale().scrollToRealTime();
    }

    // Mark the newest revealed bar — the edge of what is known — in the operator accent.
    const last = candles[candles.length - 1];
    markersRef.current?.setMarkers(
      last
        ? [{ time: toChartTime(last.ts), position: "aboveBar", color: OPERATOR, shape: "arrowDown" }]
        : [],
    );

    prevRef.current = { count: candles.length, firstTime };
  }, [candles]);

  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    priceLinesRef.current.forEach((line) => series.removePriceLine(line));
    priceLinesRef.current = [];

    const addLine = (price: number | null | undefined, color: string, title: string) => {
      if (price == null) return;
      const line = series.createPriceLine({ price, color, lineWidth: 1, axisLabelVisible: true, title });
      priceLinesRef.current.push(line);
    };

    addLine(entry, OPERATOR, "Entry");
    addLine(sl, DOWN, "SL");
    addLine(tp, UP, "TP");
  }, [entry, sl, tp, candles]);

  return <div ref={containerRef} className="h-full w-full" />;
}
