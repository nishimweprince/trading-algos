import { snapToTouchLevel } from "@/lib/constants";
import type { BaseRateCell, Occurrence } from "@/types";

/**
 * Was the operator's confidence worth anything over the prior?
 *
 * Three numbers per resolved trade answer it: what they predicted, what every
 * historical bar in that context did, and what actually happened. The gap
 * between the last two is noise; the gap between the first and the second is the
 * only thing that would make a labelled trade worth more than a lookup.
 */

/** Confidence 1–5 read as a probability, so it can sit beside a win rate. */
export function impliedRate(confidence: number): number {
  // 1 -> 10%, 5 -> 90%. Deliberately crude: the scale is ordinal, and pretending
  // otherwise would dress up an opinion as a forecast.
  return 0.1 + ((confidence - 1) / 4) * 0.8;
}

/**
 * The frozen grid cell matching the geometry the operator actually marked.
 *
 * The prior is stored across every target/stop pair precisely because the levels
 * were not known when it was frozen; this picks the cell the trade turned out to
 * occupy.
 */
export function priorCell(trade: Occurrence): BaseRateCell | null {
  const grid = trade.base_rate_at_signal;
  const { entry, sl, tp, atr_at_signal: atr } = trade;
  if (!grid?.cells?.length || entry == null || sl == null || tp == null || !atr) return null;

  const target = snapToTouchLevel(Math.abs(tp - entry) / atr);
  const stop = snapToTouchLevel(Math.abs(entry - sl) / atr);
  return (
    grid.cells.find((c) => c.target_atr === target && c.stop_atr === stop) ?? null
  );
}

export interface CalibrationRow {
  trade: Occurrence;
  confidence: number;
  predicted: number;
  prior: number;
  won: boolean;
}

/** Resolved trades carrying both a confidence and a frozen prior. */
export function calibrationRows(trades: Occurrence[]): CalibrationRow[] {
  const rows: CalibrationRow[] = [];
  for (const trade of trades) {
    if (trade.outcome_kind === "skipped" || trade.excluded) continue;
    if (trade.result !== "win" && trade.result !== "loss") continue;

    const confidence = trade.confidence;
    const cell = priorCell(trade);
    if (confidence == null || cell?.win_rate == null) continue;

    rows.push({
      trade,
      confidence,
      predicted: impliedRate(confidence),
      prior: cell.win_rate,
      won: trade.result === "win",
    });
  }
  return rows;
}

export interface CalibrationBucket {
  confidence: number;
  n: number;
  predicted: number;
  prior: number;
  actual: number;
}

/** One row per confidence level the operator has actually used. */
export function calibrationBuckets(rows: CalibrationRow[]): CalibrationBucket[] {
  const byConfidence = new Map<number, CalibrationRow[]>();
  for (const row of rows) {
    const bucket = byConfidence.get(row.confidence) ?? [];
    bucket.push(row);
    byConfidence.set(row.confidence, bucket);
  }

  return [...byConfidence.entries()]
    .sort(([a], [b]) => a - b)
    .map(([confidence, group]) => ({
      confidence,
      n: group.length,
      predicted: mean(group.map((r) => r.predicted)),
      prior: mean(group.map((r) => r.prior)),
      actual: group.filter((r) => r.won).length / group.length,
    }));
}

function mean(values: number[]): number {
  return values.reduce((sum, v) => sum + v, 0) / values.length;
}
