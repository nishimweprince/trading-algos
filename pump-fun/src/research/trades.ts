/**
 * Trade-blotter analysis shared by the baseline / re-cost / gate CLIs
 * (work plan 2026-09-25 P0.3, P1.4, P3.6). Reads the CSV the dashboard
 * exports (`/api/reports/trades.csv`), so a VPS export can be analysed
 * locally without the database.
 */
import { num, parseCsv } from './csv.ts';
import { feeBpsForMcap, mcapFromPrice } from '../positions/feeTiers.ts';
import { summarize, type TradeSummary } from './stats.ts';

export interface Trade {
  mint: string;
  exitReason: string;
  sizeSol: number;
  entryPrice: number | null;
  exitPrice: number | null;
  grossPnlSol: number;
  feesSol: number;
  netPnlSol: number;
  slippageSol: number;
  holdMs: number | null;
  score: number | null;
  relaxed: boolean;
  pumpSuffix: boolean;
  mfePct: number | null;
  maePct: number | null;
  openedAt: string;
  mode: string;
  /** Present on post-P0.5 exports. */
  mcapSolAtEntry: number | null;
  feeTierBps: number | null;
}

export function loadTrades(csvText: string): Trade[] {
  return parseCsv(csvText)
    .filter((r) => (r.state ?? 'CLOSED') === 'CLOSED' && num(r.size_sol) !== null)
    .map((r) => {
      const size = num(r.size_sol)!;
      const net = num(r.net_pnl_sol) ?? num(r.pnl_sol) ?? 0;
      return {
        mint: r.mint ?? '',
        exitReason: r.exit_reason || 'UNKNOWN',
        sizeSol: size,
        entryPrice: num(r.entry_price),
        exitPrice: num(r.exit_price),
        grossPnlSol: num(r.gross_pnl_sol) ?? net,
        feesSol: num(r.fees_sol) ?? 0,
        netPnlSol: net,
        slippageSol: num(r.slippage_sol) ?? 0,
        holdMs: num(r.hold_ms),
        score: num(r.entry_soft_score),
        relaxed: r.relaxed_risk === '1',
        pumpSuffix: (r.mint ?? '').endsWith('pump'),
        mfePct: num(r.mfe_pct),
        maePct: num(r.mae_pct),
        openedAt: r.opened_at ?? '',
        mode: r.mode ?? '',
        mcapSolAtEntry: num(r.mcap_sol_at_entry),
        feeTierBps: num(r.fee_tier_bps),
      };
    });
}

export const grossPct = (t: Trade) => (t.grossPnlSol / t.sizeSol) * 100;
export const netPct = (t: Trade) => (t.netPnlSol / t.sizeSol) * 100;

/**
 * Re-cost one trade at the real PumpSwap tier schedule (F4).
 *
 * The logged fee is decomposed as
 *   fees = loggedSwapPct x size x 2  +  txCosts  +  slippage
 * (estimatePaperFees + modelled impact), so txCosts is recovered exactly and
 * kept; only the swap component is replaced. Each leg is charged its own tier
 * on its own notional: entry on `size`, exit on the proceeds `size + gross`.
 * Market cap per leg is price x supply (pump supply 1e9) unless the export
 * already carries `mcap_sol_at_entry`.
 */
export function recostTrade(t: Trade, opts: { loggedSwapFeePct: number }): {
  entryBps: number;
  exitBps: number;
  swapFeesSol: number;
  txCostsSol: number;
  feesSol: number;
  netPnlSol: number;
} {
  const loggedSwap = (opts.loggedSwapFeePct / 100) * t.sizeSol * 2;
  const txCostsSol = Math.max(0, t.feesSol - t.slippageSol - loggedSwap);
  const entryMcap = t.mcapSolAtEntry ?? (t.entryPrice !== null ? mcapFromPrice(t.entryPrice) : null);
  const exitMcap = t.exitPrice !== null ? mcapFromPrice(t.exitPrice) : entryMcap;
  const entryBps = t.feeTierBps ?? feeBpsForMcap(entryMcap);
  const exitBps = feeBpsForMcap(exitMcap);
  const proceeds = Math.max(0, t.sizeSol + t.grossPnlSol);
  const swapFeesSol = (entryBps / 10_000) * t.sizeSol + (exitBps / 10_000) * proceeds;
  const feesSol = swapFeesSol + txCostsSol + t.slippageSol;
  return { entryBps, exitBps, swapFeesSol, txCostsSol, feesSol, netPnlSol: t.grossPnlSol - feesSol };
}

export type Cohort = { name: string; test: (t: Trade) => boolean };

export const STANDARD_COHORTS: readonly Cohort[] = [
  { name: 'All', test: () => true },
  { name: 'Strict only', test: (t) => !t.relaxed },
  { name: 'Relaxed only', test: (t) => t.relaxed },
  { name: '`pump` suffix', test: (t) => t.pumpSuffix },
  { name: 'Non-`pump` suffix', test: (t) => !t.pumpSuffix },
  { name: 'Strict + `pump`', test: (t) => !t.relaxed && t.pumpSuffix },
  { name: 'Score < 85', test: (t) => t.score !== null && t.score < 85 },
  { name: 'Score = 85', test: (t) => t.score === 85 },
  { name: 'Score > 85', test: (t) => t.score !== null && t.score > 85 },
];

export const HOLD_BUCKETS: readonly Cohort[] = [
  { name: '< 1 s', test: (t) => (t.holdMs ?? 0) < 1_000 },
  { name: '1–3 s', test: (t) => (t.holdMs ?? 0) >= 1_000 && (t.holdMs ?? 0) < 3_000 },
  { name: '3–10 s', test: (t) => (t.holdMs ?? 0) >= 3_000 && (t.holdMs ?? 0) < 10_000 },
  { name: '10–30 s', test: (t) => (t.holdMs ?? 0) >= 10_000 && (t.holdMs ?? 0) < 30_000 },
  { name: '> 30 s', test: (t) => (t.holdMs ?? 0) >= 30_000 },
];

export interface CohortRow {
  name: string;
  n: number;
  netSol: number;
  recostNetSol: number;
  grossPct: TradeSummary;
  netPct: number;
  recostNetPct: number;
  winRate: number;
  recostWinRate: number;
}

export function cohortRows(
  trades: readonly Trade[],
  cohorts: readonly Cohort[],
  recost: (t: Trade) => number,
  seed: number,
  iterations = 5_000,
): CohortRow[] {
  return cohorts.map((c) => {
    const ts = trades.filter(c.test);
    const rc = ts.map(recost);
    return {
      name: c.name,
      n: ts.length,
      netSol: ts.reduce((s, t) => s + t.netPnlSol, 0),
      recostNetSol: rc.reduce((s, x) => s + x, 0),
      grossPct: summarize(ts.map(grossPct), seed, iterations),
      netPct: ts.length ? ts.reduce((s, t) => s + netPct(t), 0) / ts.length : NaN,
      recostNetPct: ts.length ? ts.reduce((s, t, i) => s + (rc[i]! / t.sizeSol) * 100, 0) / ts.length : NaN,
      winRate: ts.length ? ts.filter((t) => t.netPnlSol > 0).length / ts.length : NaN,
      recostWinRate: ts.length ? rc.filter((x) => x > 0).length / ts.length : NaN,
    };
  });
}

export function byExitReason(trades: readonly Trade[]): Cohort[] {
  const reasons = [...new Set(trades.map((t) => t.exitReason))].sort();
  return reasons.map((r) => ({ name: r, test: (t: Trade) => t.exitReason === r }));
}
