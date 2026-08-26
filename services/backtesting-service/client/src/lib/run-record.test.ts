import { describe, expect, it } from "vitest";
import type { BacktestReport, BacktestRequest } from "./types";
import {
  backtestSettingsFilename,
  createBacktestRunRecord,
  serializeBacktestRunRecord,
} from "./run-record";

const request: BacktestRequest = {
  symbol: "XAU/USD",
  timeframe: "M15",
  source: null,
  sessions: ["tokyo", "london"],
  performance_unit: "pips",
};

const report = {
  symbol: "XAU/USD",
  timeframe: "M15",
  source: "local",
  performance_unit: "pips",
  bar_count: 120,
  effective_settings: {
    survivor_exit_mode: "mfe_trail",
    survivor_trail_activation_r: 1.5,
    survivor_trail_gap_r: 1,
    hedge_path_mode: "chronological_v2",
    cost_model: "per_session",
    max_age_hours: 24,
  },
  candle_set_sha256: "abc123",
  report_header: {
    first_bar_ts: "2026-08-01T00:00:00Z",
    last_bar_ts: "2026-08-24T00:00:00Z",
  },
} as unknown as BacktestReport;

describe("backtest run record", () => {
  it("captures immutable submitted settings and resolved run metadata", () => {
    const mutableRequest = structuredClone(request);
    const record = createBacktestRunRecord(
      mutableRequest,
      report,
      new Date("2026-08-24T21:30:45.123Z"),
    );
    mutableRequest.symbol = "CHANGED";
    mutableRequest.sessions?.push("new_york");

    expect(record.schema_version).toBe(2);
    expect(record.settings.symbol).toBe("XAU/USD");
    expect(record.settings.sessions).toEqual(["tokyo", "london"]);
    expect(record.run.resolved_source).toBe("local");
    expect(record.run.bar_count).toBe(120);
    expect(record.effective_settings.survivor_exit_mode).toBe("mfe_trail");
    expect(record.run.candle_set_sha256).toBe("abc123");
  });

  it("serializes pretty JSON with a trailing newline and safe timestamped filename", () => {
    const record = createBacktestRunRecord(
      request,
      report,
      new Date("2026-08-24T21:30:45.123Z"),
    );
    const json = serializeBacktestRunRecord(record);

    expect(json).toContain('\n  "schema_version": 2,');
    expect(json.endsWith("\n")).toBe(true);
    expect(JSON.parse(json)).toEqual(record);
    expect(backtestSettingsFilename(record)).toBe(
      "session-hedging-XAU-USD-M15-20260824T213045Z-settings.json",
    );
  });
});
