import { describe, expect, it } from "vitest";
import { calibrationBuckets, calibrationRows, impliedRate, priorCell } from "@/lib/calibration";
import type { BaseRateCell, Occurrence } from "@/types";

function cell(target: number, stop: number, winRate: number): BaseRateCell {
  return {
    target_atr: target,
    stop_atr: stop,
    wins: Math.round(winRate * 100),
    decided: 100,
    win_rate: winRate,
    expectancy_r: 0,
    effective_n: 4,
  };
}

function trade(overrides: Partial<Occurrence> = {}): Occurrence {
  return {
    id: crypto.randomUUID(),
    source: "manual",
    symbol: "XAUUSD",
    timeframe: "H1",
    ts: "2025-04-13T23:00:00Z",
    setup_id: "double_bottom",
    side: 1,
    entry: 100,
    sl: 98,
    tp: 103,
    atr_at_signal: 2,
    result: "win",
    outcome_kind: "traded",
    confidence: 4,
    base_rate_at_signal: {
      level_used: "trend_state",
      dimensions_used: ["trend_state"],
      matched_count: 100,
      median_mfe_atr: 1.9,
      median_mae_atr: -1.6,
      horizon: 24,
      side: 1,
      min_samples_required: 200,
      cells: [cell(1.5, 1.0, 0.44), cell(2.0, 1.0, 0.36), cell(1.0, 1.0, 0.54)],
    },
    ...overrides,
  } as Occurrence;
}

describe("priorCell", () => {
  it("picks the cell matching the geometry the operator actually marked", () => {
    // entry 100, sl 98, tp 103, atr 2 -> stop 1.0 ATR, target 1.5 ATR.
    expect(priorCell(trade())?.target_atr).toBe(1.5);
    expect(priorCell(trade())?.stop_atr).toBe(1.0);
  });

  it("snaps geometry that falls between rungs onto the nearest one", () => {
    // tp 103.1 -> 1.55 ATR, which the store cannot price directly.
    expect(priorCell(trade({ tp: 103.1 }))?.target_atr).toBe(1.5);
  });

  it("returns null rather than guessing when the prior or geometry is missing", () => {
    expect(priorCell(trade({ base_rate_at_signal: null }))).toBeNull();
    expect(priorCell(trade({ atr_at_signal: null }))).toBeNull();
    expect(priorCell(trade({ tp: null }))).toBeNull();
  });
});

describe("calibrationRows", () => {
  it("keeps only decided trades carrying both a confidence and a prior", () => {
    const rows = calibrationRows([
      trade(),
      trade({ result: "timeout" }),
      trade({ outcome_kind: "skipped" }),
      trade({ excluded: true }),
      trade({ confidence: null }),
      trade({ base_rate_at_signal: null }),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].prior).toBe(0.44);
    expect(rows[0].won).toBe(true);
  });
});

describe("calibrationBuckets", () => {
  it("reports predicted, prior and actual side by side per confidence level", () => {
    const rows = calibrationRows([
      trade({ confidence: 5, result: "win" }),
      trade({ confidence: 5, result: "loss" }),
      trade({ confidence: 5, result: "win" }),
      trade({ confidence: 2, result: "loss" }),
    ]);
    const buckets = calibrationBuckets(rows);

    expect(buckets.map((b) => b.confidence)).toEqual([2, 5]);

    const high = buckets[1];
    expect(high.n).toBe(3);
    expect(high.actual).toBeCloseTo(2 / 3);
    // The prior is the same context for every row here, so its mean is that rate.
    expect(high.prior).toBeCloseTo(0.44);
    expect(high.predicted).toBeCloseTo(impliedRate(5));

    expect(buckets[0].actual).toBe(0);
  });

  it("maps the ordinal confidence scale onto a probability monotonically", () => {
    expect(impliedRate(1)).toBeLessThan(impliedRate(3));
    expect(impliedRate(3)).toBeLessThan(impliedRate(5));
    expect(impliedRate(1)).toBeGreaterThan(0);
    expect(impliedRate(5)).toBeLessThan(1);
  });
});
