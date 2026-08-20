import { describe, expect, it } from "vitest";
import { backtestCsvFilename, buildBacktestCsv, buildBacktestCsvRow } from "./csv";
import type { BacktestReport } from "./types";

const report = {
  symbol: "XAUUSD",
  timeframe: "M15",
  source: "local",
  bar_count: 100,
  performance_unit: "pips",
  orb_minutes: 15,
  entry_delay_minutes: 15,
  anchor_tolerance_minutes: 15,
  realized: 13.75,
  unrealized: 0,
  equity: 100013.75,
  realized_pips: 137.5,
  unrealized_pips: 0,
  max_drawdown_pips: 398.6,
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
    expect(lines[0]).toContain("pair_pnl_pips");
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
  });

  it("builds a safe filename and includes an active session suffix", () => {
    expect(backtestCsvFilename(report, "new_york")).toBe(
      "session-hedging-XAUUSD-M15-new_york.csv",
    );
  });
});
