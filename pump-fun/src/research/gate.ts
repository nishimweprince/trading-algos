/**
 * Phase gates (work plan 2026-09-25 P3.6 / §8). Pure checks over trade
 * lists so the same code backs the CLI, the dashboard and tests. A gate is
 * met only when every criterion passes; each criterion reports its value.
 */
import { bootstrapCI, deflatedSharpe, maxDrawdown, mean, profitFactor, sum } from './stats.ts';

export interface GateTrade {
  mint: string;
  sizeSol: number;
  netPnlSol: number;
  feesSol: number;
  exitReason: string;
  relaxed: boolean;
  closedAt?: string;
}

export interface Criterion {
  name: string;
  value: string;
  target: string;
  pass: boolean;
}

export interface GateReport {
  phase: string;
  met: boolean;
  criteria: Criterion[];
}

const pct = (x: number, d = 2) => (Number.isFinite(x) ? `${x.toFixed(d)} %` : '—');

function report(phase: string, criteria: Criterion[]): GateReport {
  return { phase, met: criteria.every((c) => c.pass), criteria };
}

/** Per-trade net return, % of size. */
const netPct = (t: GateTrade) => (t.netPnlSol / t.sizeSol) * 100;

/** P2 -> P3: known-negative cohorts removed, emergency losses contained, fees honest. */
export function gateP2(trades: readonly GateTrade[]): GateReport {
  // F7's metric: emergency-exit loss as a share of the week's NET loss
  // (0.187 / 0.348 = 54 % at baseline). When the period nets positive there
  // is no net loss to share, so fall back to the share of gross losses.
  const losses = trades.filter((t) => t.netPnlSol < 0);
  const totalNet = sum(trades.map((t) => t.netPnlSol));
  const lossSol = totalNet < 0 ? -totalNet : -sum(losses.map((t) => t.netPnlSol));
  const emerg = -sum(losses.filter((t) => t.exitReason === 'EMERGENCY_EXIT').map((t) => t.netPnlSol));
  const turnover = sum(trades.map((t) => t.sizeSol));
  const feePct = turnover > 0 ? (sum(trades.map((t) => t.feesSol)) / turnover) * 100 : NaN;
  const relaxed = trades.filter((t) => t.relaxed).length;
  const nonPump = trades.filter((t) => !t.mint.endsWith('pump')).length;
  return report('P2', [
    { name: 'Relaxed-risk trades', value: String(relaxed), target: '0', pass: relaxed === 0 },
    { name: 'Non-segment-A (non-`pump`) trades', value: String(nonPump), target: '0', pass: nonPump === 0 },
    { name: 'Emergency-exit share of net loss', value: pct(lossSol > 0 ? (emerg / lossSol) * 100 : 0, 1), target: '< 25 %', pass: lossSol === 0 || emerg / lossSol < 0.25 },
    { name: 'Fees % of notional (all-in)', value: pct(feePct), target: '≤ 3.5 %', pass: Number.isFinite(feePct) && feePct <= 3.5 },
    { name: 'Trades observed', value: String(trades.length), target: '> 0', pass: trades.length > 0 },
  ]);
}

/**
 * P3 -> P4: out-of-sample edge. `trades` must be OOS by construction (a
 * frozen config run after the parameters were chosen). `trials` = number of
 * variants tried to reach it (for the Deflated Sharpe).
 */
export function gateP3(
  trades: readonly GateTrade[],
  opts: { capitalSol: number; trials: number; srVariance?: number; seed: number; minTrades?: number },
): GateReport {
  const r = trades.map(netPct);
  const n = r.length;
  const ci = n ? bootstrapCI(r, { seed: opts.seed, iterations: 10_000 }) : { point: NaN, lo: NaN, hi: NaN };
  const pf = profitFactor(trades.map((t) => t.netPnlSol));
  const dd = maxDrawdown(trades.map((t) => t.netPnlSol));
  const ddPct = opts.capitalSol > 0 ? (dd / opts.capitalSol) * 100 : NaN;
  const dsr = n > 2 ? deflatedSharpe(r, Math.max(1, opts.trials), opts.srVariance ?? 0.01).dsr : NaN;
  const min = opts.minTrades ?? 300;
  return report('P3', [
    { name: 'OOS trades', value: String(n), target: `≥ ${min}`, pass: n >= min },
    { name: 'Expectancy (net %/trade)', value: pct(n ? mean(r) : NaN), target: '> 0', pass: n > 0 && mean(r) > 0 },
    { name: '95 % CI lower bound', value: pct(ci.lo), target: '> 0', pass: ci.lo > 0 },
    { name: 'Profit factor', value: Number.isFinite(pf) ? pf.toFixed(2) : '—', target: '> 1.3', pass: pf > 1.3 },
    { name: 'Max drawdown % of capital', value: pct(ddPct, 1), target: '< 15 %', pass: ddPct < 15 },
    { name: `Deflated Sharpe (${opts.trials} trials)`, value: Number.isFinite(dsr) ? dsr.toFixed(3) : '—', target: '> 0.95', pass: dsr > 0.95 },
  ]);
}

/** P4 scale gate: live pilot results against the twin. */
export function gateP4(
  live: readonly GateTrade[],
  opts: { failedEntries: number; attemptedEntries: number; dragPctPerTrade: number | null; minTrades?: number },
): GateReport {
  const r = live.map(netPct);
  const min = opts.minTrades ?? 100;
  const failRate = opts.attemptedEntries > 0 ? (opts.failedEntries / opts.attemptedEntries) * 100 : NaN;
  return report('P4', [
    { name: 'Live trades', value: String(live.length), target: `≥ ${min}`, pass: live.length >= min },
    { name: 'Live expectancy (net %/trade)', value: pct(r.length ? mean(r) : NaN), target: '> 0', pass: r.length > 0 && mean(r) > 0 },
    { name: 'Live − twin drag (%/trade)', value: opts.dragPctPerTrade === null ? '—' : pct(opts.dragPctPerTrade), target: '< 3 %', pass: opts.dragPctPerTrade !== null && Math.abs(opts.dragPctPerTrade) < 3 },
    { name: 'Failed entries', value: pct(failRate, 1), target: '< 15 %', pass: failRate < 15 },
  ]);
}

export function renderGate(g: GateReport): string {
  return [
    `## Gate ${g.phase}: ${g.met ? 'MET ✅' : 'NOT MET ❌'}`,
    '',
    '| Criterion | Value | Target | |',
    '|---|---|---|---|',
    ...g.criteria.map((c) => `| ${c.name} | ${c.value} | ${c.target} | ${c.pass ? '✅' : '❌'} |`),
    '',
  ].join('\n');
}
