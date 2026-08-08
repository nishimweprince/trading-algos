import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { EvidencePanel } from "@/components/trade/EvidencePanel";
import type { BaseRate, BaseRateQuery, MetaReplayInference } from "@/types";

const fixture: MetaReplayInference = {
  symbol: "XAUUSD",
  timeframe: "H1",
  signal_ts: "2026-08-06T12:00:00Z",
  side: 1,
  status: "research_shadow",
  orders_enabled: false,
  calendar_coverage_ok: true,
  indicative_levels: {
    basis: "signal_close",
    reference_price: 3500,
    atr_at_signal: 20,
    stop_price: 3460,
    target_price: 3560,
    final_levels_pending: true,
  },
  predictions: [
    {
      artifact_version: "meta-v1-r3",
      role: "active",
      meta_feature_version: 1,
      probability: 0.612,
      threshold: 0.58,
      would_take: true,
      target_take_rate: 0.2,
    },
    {
      artifact_version: "meta-v2-r3",
      role: "challenger",
      meta_feature_version: 2,
      probability: 0.491,
      threshold: 0.57,
      would_take: false,
      target_take_rate: 0.2,
    },
  ],
  contract: {
    entry: "next_h1_open", horizon_bars: 24, target_atr: 3, stop_atr: 2,
  },
};

const query: BaseRateQuery = {
  symbol: "XAUUSD",
  timeframe: "H1",
  signalTs: "2026-08-06T12:00:00Z",
  horizon: 24,
  targetAtr: 1.5,
  stopAtr: 1,
};

const baseRate: BaseRate = {
  matched_count: 240,
  resolved_count: 240,
  wins: 106,
  losses: 127,
  decided: 233,
  timeouts: 7,
  win_rate: 106 / 240,
  wilson_low: 0.34,
  wilson_high: 0.55,
  expectancy_r: 0.13,
  expectancy_r_net: 0.1,
  effective_n: 24,
  net_expectancy_ci_low_r: -0.04,
  net_expectancy_ci_high_r: 0.24,
  confidence_level: 0.95,
  confidence_method: "two_week_moving_block_bootstrap",
  independent_periods: 24,
  level_used: "session+trend_state",
  dimensions_used: ["session", "trend_state"],
  requested_dimensions: ["tag_setup_id", "session", "trend_state"],
  dropped_dimensions: ["tag_setup_id"],
  fallback_used: true,
  median_mfe_atr: 1.1,
  median_mae_atr: -0.7,
  target_grid: [],
  horizon: 24,
  target_atr: 1.5,
  stop_atr: 1,
  side: 1,
  scored_side: 1,
  scored_direction: "long",
  recommendation: {
    verdict: "lean_long",
    headline: "Lean long",
    rationale: "Historical expectancy is positive, but uncertainty still crosses zero.",
    caveats: [],
    policy_version: "empirical-block-bootstrap-v2",
  },
  gross_break_even_win_rate: 0.4,
  spread_adjusted_break_even_win_rate: 0.412,
  recommendation_policy_version: "empirical-block-bootstrap-v2",
  min_samples_required: 200,
  decided_available: 240,
  min_periods_required: 20,
  periods_available: 24,
};

describe("model shadow readout", () => {
  it("separates empirical recommendation from unpromoted model economics", () => {
    const html = renderToStaticMarkup(
      createElement(EvidencePanel, {
        query,
        result: baseRate,
        modelShadow: fixture,
        modelShadowError: null,
        modelShadowLoading: false,
      }),
    );
    expect(html).toContain("Lean long");
    expect(html).toContain("empirical history");
    expect(html).toContain("Model recommendation");
    expect(html).toContain("informational only · unpromoted");
    expect(html).toContain("Recommended direction");
    expect(html).toContain("Long");
    expect(html).toContain("Would take");
    expect(html).toContain("take threshold 58.0%");
    expect(html).toContain("Positive net outcome probability");
    expect(html).toContain("Indicative levels");
    expect(html).toContain("3,460.00");
    expect(html).toContain("3,560.00");
    expect(html).toContain("Final entry, stop, and target reset from the next H1 open.");
    expect(html).toContain("meta-v1-r3");
    expect(html).not.toContain("meta-v2-r3");
    expect(html).toContain("candlestick pattern");
    expect(html).toContain("Artifact and version details");
    expect(html).not.toContain("Buy");
    expect(html).not.toContain("Sell");
  });

  it("does not present executable-looking levels for a skipped signal", () => {
    const skipped: MetaReplayInference = {
      ...fixture,
      predictions: [{ ...fixture.predictions[0], probability: 0.4, would_take: false }],
    };
    const html = renderToStaticMarkup(
      createElement(EvidencePanel, {
        query,
        result: baseRate,
        modelShadow: skipped,
        modelShadowError: null,
        modelShadowLoading: false,
      }),
    );
    expect(html).toContain("No — skip");
    expect(html).not.toContain("Indicative levels");
    expect(html).not.toContain("Take profit");
  });
});
