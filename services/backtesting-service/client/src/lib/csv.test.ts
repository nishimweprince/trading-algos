import { describe, expect, it } from "vitest";
import {
  backtestCsvFilename,
  backtestCsvSections,
  buildBacktestCsv,
  buildBacktestCsvRow,
  csvColumnsFor,
  hasHedgeLeg,
} from "./csv";
import type { BacktestReport } from "./types";

const report = {
  symbol: "XAUUSD",
  timeframe: "M15",
  source: "local",
  bar_count: 100,
  performance_unit: "pips",
  entry_mode: "hedge_pair",
  orb_minutes: 15,
  entry_delay_minutes: 15,
  anchor_tolerance_minutes: 15,
  stop_mode: "bar_range",
  fixed_stop_pips: 0,
  same_bar_resolution_rate: 0,
  same_bar_r: 0,
  survivor_tp_rate: 0.35,
  mean_loss_r: -0.94,
  breakeven_tp_rate_required: 0.32,
  tp_rate_margin_pp: 2.7,
  tp_rate_margin_pp_ci_low: -1,
  tp_rate_margin_pp_ci_high: 6,
  outcome_mix: { tp: 0.35, lock: 0.59, breakeven: 0.03, whipsaw: 0.03 },
  performance: {
    unit: "pips",
    dollars_per_pip_per_qty: null,
    qty_ref: 1,
    conversion_factor: 1,
    unit_label: "pips",
    realized: 137.5,
    unrealized: 0,
    equity: 137.5,
    gross_realized: 137.5,
    realized_cost: 0,
    net_realized: 137.5,
    gross_unrealized: 0,
    unrealized_cost: 0,
    net_unrealized: 0,
    gross_equity: 137.5,
    equity_cost: 0,
    net_equity: 137.5,
    execution_cost: 0,
    financing_cost: 0,
    max_drawdown: 398.6,
    gross_max_drawdown: 398.6,
    net_max_drawdown: 398.6,
    breakeven_per_completed_side: 3.8,
    configured_spread_per_side: 0,
    configured_execution_cost_per_side: 0,
  },
  report_header: {
    entry_mode: "hedge_pair",
    session_anchors: ["tokyo:Asia/Tokyo:09:00"],
    stop_mode: "bar_range",
    tp_mode: "fixed_r",
    rr: 3,
    partial_tp_r: 1,
    partial_fraction: 0.5,
    lock_mode: "absolute",
    lock_pips: 20,
    survivor_exit_mode: "mfe_trail",
    survivor_trail_activation_r: 1.5,
    survivor_trail_gap_r: 1,
    hedge_path_mode: "chronological_v2",
    time_exit_mode: "max_age",
    max_age_hours: 24,
    risk_mode: "fixed_qty",
    cost_model: "none",
    intrabar_mode: "m1_conservative",
    resolver_tier: 3,
    qty_ref: 1,
    firm_profile: "none",
    first_bar_ts: "2026-08-18T00:00:00Z",
    last_bar_ts: "2026-08-19T00:00:00Z",
    warmup_bars: 1,
    validation_summary: {},
    m1_bars_loaded: 0,
    m1_fallback_count: 0,
  },
  max_concurrent_structures: 3,
  median_concurrent: 1,
  equity_curve: [],
  effective_settings: {
    survivor_exit_mode: "mfe_trail",
    survivor_trail_activation_r: 1.5,
    survivor_trail_gap_r: 1,
    hedge_path_mode: "chronological_v2",
  },
  candle_set_sha256: "abc123",
  realized: 13.75,
  unrealized: 0,
  equity: 100013.75,
  realized_pips: 137.5,
  unrealized_pips: 0,
  realized_r: 0.5,
  unrealized_r: 0,
  equity_pips: 137.5,
  max_drawdown_pips: 398.6,
  max_drawdown_r: 1.2,
  realized_dollars: null,
  unrealized_dollars: null,
  equity_dollars: null,
  max_drawdown_dollars: null,
  long_wins: 0,
  long_be: 0,
  long_loss: 1,
  short_wins: 1,
  short_be: 0,
  short_loss: 0,
  locks: 1,
  open_pairs: 0,
  session_anchor_stats: [],
  trades: [],
  trade_pairs: [
    {
      id: "tokyo:2026-08-18T00:30:00Z",
      session: "tokyo",
      entry: 4421.77,
      entry_ts: "2026-08-18T00:30:00Z",
      status: "closed",
      primary: {
        side: "short",
        role: "primary",
        status: "closed",
        exit: 4359.43,
        exit_ts: "2026-08-18T15:15:00Z",
        pnl_pips: 623.4,
        pnl_dollars: null,
        mae_pips: -92.5,
        mfe_pips: 700.2,
        mae_dollars: null,
        mfe_dollars: null,
        bucket: "win",
        reason: "target, locked",
      },
      hedge: {
        side: "long",
        role: "hedge",
        status: "closed",
        exit: 4400.99,
        exit_ts: "2026-08-18T02:15:00Z",
        pnl_pips: -207.8,
        pnl_dollars: null,
        mae_pips: -207.8,
        mfe_pips: 45.3,
        mae_dollars: null,
        mfe_dollars: null,
        bucket: "loss",
        reason: "sl_or_tp",
      },
      unknown_legs: [],
      pnl_pips: 415.6,
      pnl_dollars: null,
      first_stop_ts: "2026-08-18T02:15:00Z",
      survivor_side: "short",
      survivor_post_failure_mae_pips: 15,
      survivor_post_failure_mfe_pips: 700.2,
      survivor_post_failure_mae_r: 0.07,
      survivor_post_failure_mfe_r: 3.37,
      survivor_peak_giveback_pips: 76.8,
      survivor_peak_giveback_r: 0.37,
      survivor_ratchet_armed_ts: "2026-08-18T05:15:00Z",
      survivor_ratchet_advances: 4,
      survivor_exit_efficiency: 0.89,
    },
  ],
  events: [],
} satisfies BacktestReport;

describe("backtest CSV", () => {
  it("exports one grouped row with both legs and explicit units", () => {
    const csv = buildBacktestCsv(report);
    const lines = csv.split("\r\n");
    expect(lines).toHaveLength(2);
    expect(lines[0]).toContain("primary_side");
    expect(lines[0]).toContain("hedge_side");
    expect(lines[0]).toContain("survivor_post_failure_mfe_r");
    expect(lines[0]).toContain("pair_pnl_pips");
    expect(lines[0]).toContain("pair_gross_pnl_pips,pair_cost_pips,pair_net_pnl_pips");
    expect(lines[0]).toContain("primary_mae_pips,primary_mfe_pips");
    expect(lines[0]).toContain("hedge_mae_pips,hedge_mfe_pips");
    expect(lines[1]).toContain("short,closed,4359.43,2026-08-18T15:15:00Z,win,623.4");
    expect(lines[1]).toContain("long,closed,4400.99,2026-08-18T02:15:00Z,loss,-207.8");
    expect(lines[1]).toContain('"target, locked"');
    expect(lines[1]).toContain("623.4,,-92.5,700.2,,");
    expect(lines[1]).toContain("-207.8,,-207.8,45.3,,");
  });

  it("builds one row object with the same fields as the CSV export", () => {
    const row = buildBacktestCsvRow(report, report.trade_pairs[0]);
    expect(row.symbol).toBe("XAUUSD");
    expect(row.primary_side).toBe("short");
    expect(row.hedge_reason).toBe("sl_or_tp");
    expect(row.pair_pnl_pips).toBe(415.6);
    expect(row.pair_gross_pnl_pips).toBe(415.6);
    expect(row.pair_cost_pips).toBe(0);
    expect(row.pair_net_pnl_pips).toBe(415.6);
    expect(row.survivor_ratchet_advances).toBe(4);
  });

  it("drops the hedge columns for single-sided entry modes", () => {
    // An OCO bracket cancels its sibling on fill, so a hedge leg cannot exist. Emitting
    // structurally empty hedge_* columns reads as a broken hedge rather than as no hedge.
    const oco = { ...report, entry_mode: "oco_bracket" } as BacktestReport;
    const lines = buildBacktestCsv(oco).split("\r\n");
    expect(lines[0]).toContain("primary_side");
    expect(lines[0]).not.toContain("hedge_");
    expect(lines[0]).not.toContain("survivor_");
    expect(lines[0]).toContain("pair_net_pnl_pips");
    expect(lines).toHaveLength(2);
  });

  it("keeps the hedge columns for genuinely two-sided modes", () => {
    expect(hasHedgeLeg("hedge_pair")).toBe(true);
    expect(hasHedgeLeg("contingent_hedge")).toBe(true);
    expect(hasHedgeLeg("oco_bracket")).toBe(false);
    expect(hasHedgeLeg("synthetic_breakout")).toBe(false);
    expect(csvColumnsFor("hedge_pair")).toEqual([...csvColumnsFor("hedge_pair")]);
    expect(csvColumnsFor("oco_bracket").some((c) => c.startsWith("hedge_"))).toBe(false);
  });

  it("hides the hedge detail section for single-sided modes", () => {
    const titles = backtestCsvSections("oco_bracket").map((section) => section.title);
    expect(titles).not.toContain("Hedge leg");
    expect(backtestCsvSections("hedge_pair").map((s) => s.title)).toContain("Hedge leg");
    expect(backtestCsvSections("hedge_pair").map((s) => s.title)).toContain("Survivor path");
    expect(titles).not.toContain("Survivor path");
  });

  it("builds a safe filename and includes an active session suffix", () => {
    expect(backtestCsvFilename(report, "new_york")).toBe(
      "session-hedging-XAUUSD-M15-new_york.csv",
    );
  });
});
