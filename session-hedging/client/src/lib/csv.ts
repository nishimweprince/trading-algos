import { formatDollars, formatPips, formatPrice, formatWhen } from "./format";
import { SESSION_LABEL, type BacktestReport, type TradePairLeg, type TradePairResult } from "./types";

export const BACKTEST_CSV_COLUMNS = [
  "symbol",
  "timeframe",
  "source",
  "performance_unit",
  "pair_id",
  "session",
  "entry_time",
  "entry_price",
  "pair_status",
  "primary_side",
  "primary_status",
  "primary_exit_price",
  "primary_exit_time",
  "primary_result",
  "primary_pnl_pips",
  "primary_pnl_dollars",
  "primary_mae_pips",
  "primary_mfe_pips",
  "primary_mae_dollars",
  "primary_mfe_dollars",
  "primary_reason",
  "primary_gross_pnl_pips",
  "primary_cost_pips",
  "primary_net_pnl_pips",
  "hedge_side",
  "hedge_status",
  "hedge_exit_price",
  "hedge_exit_time",
  "hedge_result",
  "hedge_pnl_pips",
  "hedge_pnl_dollars",
  "hedge_mae_pips",
  "hedge_mfe_pips",
  "hedge_mae_dollars",
  "hedge_mfe_dollars",
  "hedge_reason",
  "hedge_gross_pnl_pips",
  "hedge_cost_pips",
  "hedge_net_pnl_pips",
  "pair_pnl_pips",
  "pair_pnl_dollars",
  "pair_gross_pnl_pips",
  "pair_cost_pips",
  "pair_net_pnl_pips",
] as const;

export type BacktestCsvColumn = (typeof BACKTEST_CSV_COLUMNS)[number];
export type BacktestCsvRow = Record<BacktestCsvColumn, string | number | null>;

export type BacktestCsvContext = Pick<
  BacktestReport,
  "symbol" | "timeframe" | "source" | "performance_unit"
>;

export function buildBacktestCsvRow(
  report: BacktestCsvContext,
  pair: TradePairResult,
): BacktestCsvRow {
  return {
    symbol: report.symbol,
    timeframe: report.timeframe,
    source: report.source,
    performance_unit: report.performance_unit,
    pair_id: pair.id,
    session: pair.session,
    entry_time: pair.entry_ts,
    entry_price: pair.entry,
    pair_status: pair.status,
    ...primaryLegFields(pair.primary),
    ...hedgeLegFields(pair.hedge),
    pair_pnl_pips: pair.pnl_pips,
    pair_pnl_dollars: pair.pnl_dollars,
    pair_gross_pnl_pips: pair.gross_pnl_pips ?? pair.pnl_pips,
    pair_cost_pips: pair.cost_pips ?? 0,
    pair_net_pnl_pips: pair.net_pnl_pips ?? pair.gross_pnl_pips ?? pair.pnl_pips,
  };
}

export function csvColumnLabel(column: BacktestCsvColumn): string {
  return column
    .split("_")
    .map((part) => (part === "mae" || part === "mfe" ? part.toUpperCase() : part))
    .join(" ")
    .replace(/^pair id$/, "Pair ID")
    .replace(/^pair pnl pips$/, "Pair P&L (pips)")
    .replace(/^pair pnl dollars$/, "Pair P&L ($)")
    .replace(/ pnl pips$/, " P&L (pips)")
    .replace(/ pnl dollars$/, " P&L ($)")
    .replace(/ mae pips$/, " MAE (pips)")
    .replace(/ mfe pips$/, " MFE (pips)")
    .replace(/ mae dollars$/, " MAE ($)")
    .replace(/ mfe dollars$/, " MFE ($)")
    .replace(/^entry time$/, "Entry time")
    .replace(/^entry price$/, "Entry price")
    .replace(/^pair status$/, "Pair status")
    .replace(/^performance unit$/, "Performance unit")
    .replace(/^exit time$/, "Exit time")
    .replace(/^exit price$/, "Exit price");
}

export function formatCsvDetailValue(
  column: BacktestCsvColumn,
  value: string | number | null,
): string {
  if (value === null || value === undefined || value === "") return "—";
  if (column.endsWith("_time") && typeof value === "string") return formatWhen(value);
  if (column.endsWith("_price") && typeof value === "number") return formatPrice(value);
  if (column.endsWith("_pips") && typeof value === "number") return formatPips(value);
  if (column.endsWith("_dollars") && typeof value === "number") return formatDollars(value);
  if (column === "session" && typeof value === "string") {
    return SESSION_LABEL[value] ?? value;
  }
  return String(value);
}

export const BACKTEST_CSV_SECTIONS: { title: string; columns: BacktestCsvColumn[] }[] = [
  {
    title: "Run",
    columns: ["symbol", "timeframe", "source", "performance_unit"],
  },
  {
    title: "Pair",
    columns: [
      "pair_id",
      "session",
      "entry_time",
      "entry_price",
      "pair_status",
      "pair_pnl_pips",
      "pair_pnl_dollars",
    ],
  },
  {
    title: "Primary leg",
    columns: BACKTEST_CSV_COLUMNS.filter((column) => column.startsWith("primary_")),
  },
  {
    title: "Hedge leg",
    columns: BACKTEST_CSV_COLUMNS.filter((column) => column.startsWith("hedge_")),
  },
];

export function buildBacktestCsv(
  report: BacktestReport,
  pairs: TradePairResult[] = report.trade_pairs,
): string {
  const rows = pairs.map((pair) =>
    BACKTEST_CSV_COLUMNS.map((column) => csvCell(buildBacktestCsvRow(report, pair)[column])).join(","),
  );
  return [BACKTEST_CSV_COLUMNS.join(","), ...rows].join("\r\n");
}

export function downloadBacktestCsv(
  report: BacktestReport,
  pairs: TradePairResult[] = report.trade_pairs,
  session: string | null = null,
): void {
  const blob = new Blob(["\uFEFF", buildBacktestCsv(report, pairs)], {
    type: "text/csv;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = backtestCsvFilename(report, session);
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export function backtestCsvFilename(
  report: Pick<BacktestReport, "symbol" | "timeframe">,
  session: string | null = null,
): string {
  const suffix = session ? `-${safeFilenamePart(session)}` : "";
  return `session-hedging-${safeFilenamePart(report.symbol)}-${report.timeframe}${suffix}.csv`;
}

function primaryLegFields(leg: TradePairLeg | null): Pick<
  BacktestCsvRow,
  | "primary_side"
  | "primary_status"
  | "primary_exit_price"
  | "primary_exit_time"
  | "primary_result"
  | "primary_pnl_pips"
  | "primary_pnl_dollars"
  | "primary_mae_pips"
  | "primary_mfe_pips"
  | "primary_mae_dollars"
  | "primary_mfe_dollars"
  | "primary_reason"
  | "primary_gross_pnl_pips"
  | "primary_cost_pips"
  | "primary_net_pnl_pips"
> {
  return {
    primary_side: leg?.side ?? null,
    primary_status: leg?.status ?? null,
    primary_exit_price: leg?.exit ?? null,
    primary_exit_time: leg?.exit_ts ?? null,
    primary_result: leg?.bucket ?? null,
    primary_pnl_pips: leg?.pnl_pips ?? null,
    primary_pnl_dollars: leg?.pnl_dollars ?? null,
    primary_mae_pips: leg?.mae_pips ?? null,
    primary_mfe_pips: leg?.mfe_pips ?? null,
    primary_mae_dollars: leg?.mae_dollars ?? null,
    primary_mfe_dollars: leg?.mfe_dollars ?? null,
    primary_reason: leg?.reason ?? null,
    primary_gross_pnl_pips: leg ? (leg.gross_pnl_pips ?? leg.pnl_pips) : null,
    primary_cost_pips: leg ? (leg.cost_pips ?? 0) : null,
    primary_net_pnl_pips: leg
      ? (leg.net_pnl_pips ?? leg.gross_pnl_pips ?? leg.pnl_pips)
      : null,
  };
}

function hedgeLegFields(leg: TradePairLeg | null): Pick<
  BacktestCsvRow,
  | "hedge_side"
  | "hedge_status"
  | "hedge_exit_price"
  | "hedge_exit_time"
  | "hedge_result"
  | "hedge_pnl_pips"
  | "hedge_pnl_dollars"
  | "hedge_mae_pips"
  | "hedge_mfe_pips"
  | "hedge_mae_dollars"
  | "hedge_mfe_dollars"
  | "hedge_reason"
  | "hedge_gross_pnl_pips"
  | "hedge_cost_pips"
  | "hedge_net_pnl_pips"
> {
  return {
    hedge_side: leg?.side ?? null,
    hedge_status: leg?.status ?? null,
    hedge_exit_price: leg?.exit ?? null,
    hedge_exit_time: leg?.exit_ts ?? null,
    hedge_result: leg?.bucket ?? null,
    hedge_pnl_pips: leg?.pnl_pips ?? null,
    hedge_pnl_dollars: leg?.pnl_dollars ?? null,
    hedge_mae_pips: leg?.mae_pips ?? null,
    hedge_mfe_pips: leg?.mfe_pips ?? null,
    hedge_mae_dollars: leg?.mae_dollars ?? null,
    hedge_mfe_dollars: leg?.mfe_dollars ?? null,
    hedge_reason: leg?.reason ?? null,
    hedge_gross_pnl_pips: leg ? (leg.gross_pnl_pips ?? leg.pnl_pips) : null,
    hedge_cost_pips: leg ? (leg.cost_pips ?? 0) : null,
    hedge_net_pnl_pips: leg
      ? (leg.net_pnl_pips ?? leg.gross_pnl_pips ?? leg.pnl_pips)
      : null,
  };
}

function csvCell(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "";
  let text = String(value);
  if (typeof value === "string" && /^[=+\-@]/.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function safeFilenamePart(value: string): string {
  return value.trim().replaceAll(/[^a-zA-Z0-9_-]+/g, "-").replaceAll(/^-+|-+$/g, "") || "result";
}
