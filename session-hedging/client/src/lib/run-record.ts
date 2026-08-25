import type { BacktestReport, BacktestRequest } from "./types";

export interface BacktestRunRecord {
  schema_version: 1;
  completed_at: string;
  settings: BacktestRequest;
  run: {
    symbol: string;
    timeframe: string;
    performance_unit: string;
    resolved_source: string;
    first_bar_ts: string | null;
    last_bar_ts: string | null;
    bar_count: number;
  };
}

export function createBacktestRunRecord(
  settings: BacktestRequest,
  report: BacktestReport,
  completedAt: Date = new Date(),
): BacktestRunRecord {
  return {
    schema_version: 1,
    completed_at: completedAt.toISOString(),
    settings: structuredClone(settings),
    run: {
      symbol: report.symbol,
      timeframe: report.timeframe,
      performance_unit: report.performance_unit,
      resolved_source: report.source,
      first_bar_ts: report.report_header.first_bar_ts,
      last_bar_ts: report.report_header.last_bar_ts,
      bar_count: report.bar_count,
    },
  };
}

export function serializeBacktestRunRecord(record: BacktestRunRecord): string {
  return `${JSON.stringify(record, null, 2)}\n`;
}

export function backtestSettingsFilename(record: BacktestRunRecord): string {
  const timestamp = record.completed_at.replace(/\.\d{3}Z$/, "Z").replaceAll(/[-:]/g, "");
  return `session-hedging-${safeFilenamePart(record.run.symbol)}-${safeFilenamePart(
    record.run.timeframe,
  )}-${safeFilenamePart(timestamp)}-settings.json`;
}

export function downloadBacktestRunRecord(record: BacktestRunRecord): void {
  const blob = new Blob([serializeBacktestRunRecord(record)], {
    type: "application/json;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = backtestSettingsFilename(record);
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function safeFilenamePart(value: string): string {
  return value.trim().replace(/[^a-zA-Z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "run";
}
