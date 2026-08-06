import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { EvidencePanel } from "@/components/trade/EvidencePanel";
import type { BaseRate, BaseRateQuery, OutcomeShadow } from "@/types";

const fixture: OutcomeShadow = {
  long: {
    direction: "long", side: 1, p_win: 0.612, p_loss: 0.238, p_timeout: 0.15,
    expected_gross_r: 0.68, estimated_spread_cost_r: 0.03, expected_net_r: 0.65,
    gross_break_even_p_win: 0.4, spread_adjusted_break_even_p_win: 0.352,
    edge_over_break_even: 0.26,
  },
  short: {
    direction: "short", side: -1, p_win: 0.291, p_loss: 0.509, p_timeout: 0.2,
    expected_gross_r: -0.0725, estimated_spread_cost_r: 0.03, expected_net_r: -0.1025,
    gross_break_even_p_win: 0.4, spread_adjusted_break_even_p_win: 0.332,
    edge_over_break_even: -0.041,
  },
  contract: {
    timeframe: "H1", horizon_bars: 24, target_atr: 1.5, stop_atr: 1,
    spread_pips_assumed: 3, atr_at_signal: 10,
  },
  model_version: "hist-gradient-boosting",
  artifact_version: "r2",
  schema_sha256: "abc",
  outcome_feature_version: "1",
  feature_version: "2.0.0",
  bar_feature_version: "1.2.0",
  status: "pilot_shadow",
  pilot: true,
  promoted: false,
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
    expect(html).toContain("Model shadow");
    expect(html).toContain("informational only · unpromoted");
    expect(html).toContain("break-even 35.2%");
    expect(html).toContain("candlestick pattern");
    expect(html).toContain("Artifact and version details");
    expect(html).not.toContain("Buy");
    expect(html).not.toContain("Sell");
  });
});
