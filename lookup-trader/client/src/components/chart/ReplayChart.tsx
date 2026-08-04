import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import {
  CandlestickSeries,
  HistogramSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type MouseEventParams,
  type Time,
} from "lightweight-charts";
import { LEVEL_LABELS, type PriceLevelKey } from "@/components/chart/PriceLines";
import { useReplayStore } from "@/hooks/useReplay";
import { inferPriceDigits } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { BarFeatureRow, Candle } from "@/types";

interface ReplayChartProps {
  candles: Candle[];
  blinded?: boolean;
  entry?: number | null;
  sl?: number | null;
  tp?: number | null;
  /**
   * Precomputed per-bar context for the strip below the price pane. Only the
   * causal half — these values are derived from bars at or before their own
   * anchor, so drawing them reveals nothing the cursor has not already passed.
   */
  features?: BarFeatureRow[];
  /** Median peak/dip in this context, as prices. Aggregates over other bars. */
  typicalPeak?: number | null;
  typicalDip?: number | null;
  /** Level the operator is placing — the chart takes the next click for it. */
  armed?: PriceLevelKey | null;
  onPickPrice?: (field: PriceLevelKey, price: number) => void;
}

export interface ReplayChartHandle {
  takeScreenshot: () => Promise<Blob | null>;
}

/** Bars of empty space kept to the right of the newest candle. */
const RIGHT_OFFSET_BARS = 12;

/** How near an OHLC value a click has to land to snap onto it. */
const SNAP_PX = 6;

const OPERATOR = "#38bdf8";
const UP = "#22c55e";
const DOWN = "#ef4444";
const NEUTRAL = "#a1a1aa";

/** Height of the context strip, as a share of the chart. */
const STRIP_RATIO = 0.16;

/** Volatility regime, shown as tint so the strip carries two dimensions. */
const ATR_COLORS: Record<string, string> = {
  low: "#3f3f46",
  mid: "#52525b",
  high: "#a16207",
};

const LEVEL_COLORS: Record<PriceLevelKey, string> = { entry: OPERATOR, sl: DOWN, tp: UP };

function toChartTime(ts: string): Time {
  return Math.floor(new Date(ts).getTime() / 1000) as Time;
}

function toBar(c: Candle): CandlestickData<Time> {
  return { time: toChartTime(c.ts), open: c.open, high: c.high, low: c.low, close: c.close };
}

export const ReplayChart = forwardRef<ReplayChartHandle, ReplayChartProps>(function ReplayChart(
  {
    candles,
    blinded = false,
    entry,
    sl,
    tp,
    features,
    typicalPeak,
    typicalDip,
    armed = null,
    onPickPrice,
  },
  ref,
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const stripRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  // Deliberately not in priceLinesRef: that array is wiped on every bar reveal.
  const previewLineRef = useRef<IPriceLine | null>(null);
  const prevRef = useRef<{ count: number; firstTime: Time | null }>({ count: 0, firstTime: null });
  const [revealLineLeft, setRevealLineLeft] = useState<number | null>(null);

  const cursor = useReplayStore((s) => s.cursor);

  const updateRevealLineRef = useRef(() => {});
  updateRevealLineRef.current = () => {
    const chart = chartRef.current;
    const last = candles[candles.length - 1];
    if (!chart || !last) {
      setRevealLineLeft(null);
      return;
    }
    const x = chart.timeScale().timeToCoordinate(toChartTime(last.ts));
    setRevealLineLeft(x);
  };

  useImperativeHandle(ref, () => ({
    takeScreenshot: async () => {
      const chart = chartRef.current;
      if (!chart) return null;
      const canvas = chart.takeScreenshot();
      return new Promise<Blob | null>((resolve) => {
        canvas.toBlob((blob) => resolve(blob), "image/png");
      });
    },
  }));

  // The pointer handlers are subscribed once, with the chart, so they read the
  // live arming state through refs rather than resubscribing on every render.
  const armedRef = useRef(armed);
  const onPickPriceRef = useRef(onPickPrice);
  const digitsRef = useRef(5);
  useEffect(() => {
    armedRef.current = armed;
    onPickPriceRef.current = onPickPrice;
  }, [armed, onPickPrice]);

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
    /**
     * Context strip in its own pane rather than tinting the candles: candle
     * colour already means up/down, and overloading it would destroy the one
     * thing every reader of a chart takes for granted. Bar height is the
     * efficiency ratio — is price going anywhere — tinted by volatility regime.
     */
    const strip = chart.addSeries(
      HistogramSeries,
      {
        priceFormat: { type: "volume" },
        priceLineVisible: false,
        lastValueVisible: false,
      },
      1,
    );

    chartRef.current = chart;
    seriesRef.current = series;
    stripRef.current = strip;
    markersRef.current = createSeriesMarkers(series, []);
    prevRef.current = { count: 0, firstTime: null };

    /**
     * Price under the pointer, pulled onto the hovered bar's open/high/low/close
     * when it lands within SNAP_PX of one — a stop belongs exactly on the swing
     * low, not a pixel off it. Distance is measured in pixels so the tolerance
     * means the same thing on an FX pair and on an index.
     */
    const priceAt = (param: MouseEventParams<Time>): number | null => {
      const point = param.point;
      if (!point) return null;
      const raw = series.coordinateToPrice(point.y);
      if (raw == null) return null;

      const bar = param.seriesData.get(series) as CandlestickData<Time> | undefined;
      if (bar) {
        let best: { price: number; distance: number } | null = null;
        for (const price of [bar.open, bar.high, bar.low, bar.close]) {
          const y = series.priceToCoordinate(price);
          if (y == null) continue;
          const distance = Math.abs(y - point.y);
          if (distance <= SNAP_PX && (!best || distance < best.distance)) best = { price, distance };
        }
        if (best) return best.price;
      }
      return Number(raw.toFixed(digitsRef.current));
    };

    const clearPreview = () => {
      if (!previewLineRef.current) return;
      series.removePriceLine(previewLineRef.current);
      previewLineRef.current = null;
    };

    const onCrosshairMove = (param: MouseEventParams<Time>) => {
      const field = armedRef.current;
      if (!field) return;
      const price = priceAt(param);
      // Pointer left the pane — drop the preview rather than freeze it mid-air.
      if (price == null) return clearPreview();

      if (previewLineRef.current) {
        previewLineRef.current.applyOptions({ price });
      } else {
        previewLineRef.current = series.createPriceLine({
          price,
          color: LEVEL_COLORS[field],
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: LEVEL_LABELS[field],
        });
      }
    };

    const onClick = (param: MouseEventParams<Time>) => {
      const field = armedRef.current;
      if (!field) return;
      const price = priceAt(param);
      if (price == null) return;
      onPickPriceRef.current?.(field, price);
    };

    chart.subscribeCrosshairMove(onCrosshairMove);
    chart.subscribeClick(onClick);

    const ro = new ResizeObserver(() => {
      if (containerRef.current) {
        const height = containerRef.current.clientHeight;
        chart.applyOptions({ width: containerRef.current.clientWidth, height });
        // Panes are sized in pixels, so the strip has to be re-proportioned
        // whenever the container changes rather than set once.
        chart.panes()[1]?.setHeight(Math.max(40, Math.round(height * STRIP_RATIO)));
      }
      updateRevealLineRef.current();
    });
    ro.observe(containerRef.current);

    const onRangeChange = () => updateRevealLineRef.current();
    chart.timeScale().subscribeVisibleLogicalRangeChange(onRangeChange);

    return () => {
      ro.disconnect();
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRangeChange);
      chart.unsubscribeCrosshairMove(onCrosshairMove);
      chart.unsubscribeClick(onClick);
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      stripRef.current = null;
      markersRef.current = null;
      priceLinesRef.current = [];
      previewLineRef.current = null;
    };
  }, [blinded]);

  useEffect(() => {
    updateRevealLineRef.current();
  }, [candles, cursor]);

  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;

    // Left alone the library assumes two decimals, which renders an FX quote as
    // 1.08 on the axis and rounds picked levels into uselessness.
    const digits = inferPriceDigits(candles);
    if (digits !== digitsRef.current) {
      digitsRef.current = digits;
      series.applyOptions({
        priceFormat: { type: "price", precision: digits, minMove: 10 ** -digits },
      });
    }

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
    updateRevealLineRef.current();
  }, [candles]);

  /**
   * The strip is clipped to the revealed bars, not to the fetched range: the
   * features arrive for the whole session window, and drawing past the cursor
   * would put a mark where the chart is deliberately showing nothing.
   */
  useEffect(() => {
    const strip = stripRef.current;
    if (!strip) return;

    if (!features?.length || candles.length === 0) {
      strip.setData([]);
      return;
    }

    const revealed = new Set(candles.map((c) => c.ts));
    strip.setData(
      features
        .filter((f) => revealed.has(f.ts) && f.efficiency_ratio != null)
        .map((f) => ({
          time: toChartTime(f.ts),
          value: f.efficiency_ratio as number,
          color: ATR_COLORS[f.atr_bucket ?? ""] ?? NEUTRAL,
        })),
    );
  }, [features, candles]);

  // Drop the preview whenever the armed field changes — disarming must not leave
  // a dangling line, and switching Stop→Target must not keep the old colour and
  // title. The next pointer move redraws it for the new field.
  useEffect(() => {
    const series = seriesRef.current;
    if (series && previewLineRef.current) series.removePriceLine(previewLineRef.current);
    previewLineRef.current = null;
  }, [armed]);

  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    priceLinesRef.current.forEach((line) => series.removePriceLine(line));
    priceLinesRef.current = [];

    const addLine = (
      price: number | null | undefined,
      color: string,
      title: string,
      style: LineStyle = LineStyle.Solid,
    ) => {
      if (price == null) return;
      const line = series.createPriceLine({
        price,
        color,
        lineWidth: 1,
        lineStyle: style,
        axisLabelVisible: true,
        title,
      });
      priceLinesRef.current.push(line);
    };

    addLine(entry, OPERATOR, "Entry");
    addLine(sl, DOWN, "SL");
    addLine(tp, UP, "TP");
    // Where price typically got to from bars in this context — medians over
    // other bars anchored at this close, not a forecast of this one. Dotted so
    // they never read as levels the operator placed.
    addLine(typicalPeak, NEUTRAL, "Typical peak", LineStyle.Dotted);
    addLine(typicalDip, NEUTRAL, "Typical dip", LineStyle.Dotted);
  }, [entry, sl, tp, typicalPeak, typicalDip, candles]);

  const revealLabel = blinded
    ? `Bar ${cursor + 1} · •••`
    : `Bar ${cursor + 1} · revealed through here`;

  return (
    <div className="relative h-full w-full">
      <div ref={containerRef} className={cn("h-full w-full", armed && "cursor-crosshair")} />
      <div className="pointer-events-none absolute inset-0">
        {candles.length > 0 && (
          <div className="absolute left-2 top-2 rounded border border-operator/30 bg-zinc-950/80 px-2 py-1 text-[10px] text-operator backdrop-blur-sm">
            {revealLabel}
          </div>
        )}
        {armed && (
          <div className="absolute left-1/2 top-2 -translate-x-1/2 rounded border border-zinc-700 bg-zinc-950/90 px-3 py-1 text-xs text-zinc-300 backdrop-blur-sm">
            Click chart to set {LEVEL_LABELS[armed]} · Esc to cancel
          </div>
        )}
        {revealLineLeft != null && (
          <div
            className="absolute bottom-0 top-0 w-px bg-operator/40"
            style={{ left: revealLineLeft }}
          />
        )}
      </div>
    </div>
  );
});
