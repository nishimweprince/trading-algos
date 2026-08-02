import { useEffect, useRef } from "react";
import { CandlestickSeries, createChart, type IChartApi, type ISeriesApi, type Time } from "lightweight-charts";
import type { Candle } from "@/types";

interface ReplayChartProps {
  candles: Candle[];
  blinded?: boolean;
  entry?: number | null;
  sl?: number | null;
  tp?: number | null;
}

function toChartTime(ts: string): Time {
  return Math.floor(new Date(ts).getTime() / 1000) as Time;
}

export function ReplayChart({ candles, blinded = false, entry, sl, tp }: ReplayChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const priceLinesRef = useRef<ReturnType<ISeriesApi<"Candlestick">["createPriceLine"]>[]>([]);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: { background: { color: "#09090b" }, textColor: "#a1a1aa" },
      grid: { vertLines: { color: "#27272a" }, horzLines: { color: "#27272a" } },
      rightPriceScale: { borderColor: "#27272a" },
      timeScale: { borderColor: "#27272a", timeVisible: !blinded, secondsVisible: false },
      crosshair: { mode: 1 },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderVisible: false,
      wickUpColor: "#22c55e",
      wickDownColor: "#ef4444",
    });
    chartRef.current = chart;
    seriesRef.current = series;

    const ro = new ResizeObserver(() => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth, height: containerRef.current.clientHeight });
      }
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, [blinded]);

  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    series.setData(
      candles.map((c) => ({
        time: toChartTime(c.ts),
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      })),
    );
    chartRef.current?.timeScale().scrollToRealTime();
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

    addLine(entry, "#38bdf8", "Entry");
    addLine(sl, "#ef4444", "SL");
    addLine(tp, "#22c55e", "TP");
  }, [entry, sl, tp, candles]);

  return <div ref={containerRef} className="h-full w-full min-h-[400px]" />;
}
