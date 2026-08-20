import type { ReactNode } from "react";
import { toDateKey } from "@/lib/format";
import type { CandleBounds, Session } from "@/types";
import { cn } from "@/lib/utils";

const STEPS = [
  { title: "Configure session", detail: "Pick symbol, timeframe, and date window in the bar above." },
  { title: "Replay bars", detail: "Play or step forward — only revealed bars appear on the chart." },
  { title: "Mark trade", detail: "Set entry, stop, and target, then submit to build the record." },
] as const;

const SKELETON_HEIGHTS = [42, 68, 55, 80, 48, 72, 58, 65, 50, 75, 44, 60];

interface ChartViewportStateProps {
  session: Session | null;
  isLoading: boolean;
  error: Error | null;
  dataRange: CandleBounds | undefined;
  children: ReactNode;
  chartReady: boolean;
}

function formatDataRange(bounds: CandleBounds | undefined): string | null {
  if (!bounds?.min_ts || !bounds?.max_ts) return null;
  return `${toDateKey(new Date(bounds.min_ts))} → ${toDateKey(new Date(bounds.max_ts))}`;
}

function ChartSkeleton() {
  return (
    <div
      className="absolute inset-0 flex items-end justify-center gap-1 px-8 pb-12"
      aria-busy="true"
      aria-label="Loading candles"
    >
      {SKELETON_HEIGHTS.map((h, i) => (
        <div
          key={i}
          className="w-2 flex-1 max-w-3 rounded-sm bg-zinc-800 motion-safe:animate-pulse"
          style={{ height: `${h}%` }}
        />
      ))}
    </div>
  );
}

function ChartEmptyState({ dataRange }: { dataRange: CandleBounds | undefined }) {
  const rangeLabel = formatDataRange(dataRange);
  const noData = dataRange?.bar_count === 0;

  return (
    <div className="absolute inset-0 flex items-center justify-center p-6">
      <div className="max-w-md space-y-5 text-center">
        <div className="space-y-1">
          <p className="text-sm font-medium text-zinc-300">Start a replay session</p>
          <p className="text-xs text-zinc-500">Bars past the cursor stay hidden until you step forward.</p>
        </div>

        <ol className="space-y-3 text-left text-xs text-zinc-400">
          {STEPS.map((step, i) => (
            <li key={step.title} className="flex gap-3">
              <span className="tnum flex h-5 w-5 shrink-0 items-center justify-center rounded border border-zinc-800 text-[10px] text-zinc-500">
                {i + 1}
              </span>
              <span>
                <span className="font-medium text-zinc-300">{step.title}</span>
                {" — "}
                {step.detail}
              </span>
            </li>
          ))}
        </ol>

        {rangeLabel && !noData && (
          <p className="tnum text-xs text-zinc-500">Data on disk: {rangeLabel}</p>
        )}
        {noData && (
          <p className="text-xs text-zinc-500">
            No candles found. Ingest HistData CSVs, then restart the session.
          </p>
        )}
      </div>
    </div>
  );
}

function ChartErrorState({
  error,
  dataRange,
}: {
  error: Error;
  dataRange: CandleBounds | undefined;
}) {
  const rangeLabel = formatDataRange(dataRange);

  return (
    <div className="absolute inset-0 flex items-center justify-center p-6">
      <div className="max-w-sm space-y-2 text-center">
        <p className="text-sm font-medium text-[var(--color-destructive)]">Could not load candles</p>
        <p className="text-xs text-zinc-400">{error.message}</p>
        <p className="text-xs text-zinc-500">
          Check that your session dates fall within the data available range.
          {rangeLabel ? ` Available: ${rangeLabel}.` : ""}
        </p>
      </div>
    </div>
  );
}

export function ChartViewportState({
  session,
  isLoading,
  error,
  dataRange,
  children,
  chartReady,
}: ChartViewportStateProps) {
  const showChart = chartReady && session && !isLoading && !error;

  return (
    <div className="relative min-h-0 flex-1 border border-zinc-800 bg-[var(--color-background)]">
      {isLoading && <ChartSkeleton />}
      {error && !isLoading && <ChartErrorState error={error} dataRange={dataRange} />}
      {!session && !isLoading && !error && <ChartEmptyState dataRange={dataRange} />}

      {showChart && (
        <div
          className={cn(
            "absolute inset-0 motion-safe:transition-opacity motion-safe:duration-200",
            showChart ? "opacity-100" : "opacity-0",
          )}
        >
          {children}
        </div>
      )}
    </div>
  );
}
