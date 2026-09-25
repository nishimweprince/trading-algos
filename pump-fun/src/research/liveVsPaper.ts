/**
 * Live vs paper realization (work plan 2026-09-25 P1.4 gate input).
 *
 * Compares how each exit reason REALIZED live against the paper blotter:
 * the gap is the fill/latency drag the honest simulator must reproduce.
 * Reads the strategy-week `trades-*.csv` (live) and a trade blotter (paper).
 */
import { num, parseCsv } from './csv.ts';
import { mean, median } from './stats.ts';
import type { Trade } from './trades.ts';

export interface LiveTrade {
  exitReason: string;
  netPct: number;
  detectToOpenMs: number | null;
  exitConfirmMs: number | null;
  holdMs: number | null;
}

export function loadLiveTrades(csvText: string): LiveTrade[] {
  return parseCsv(csvText)
    .filter((r) => (r.mode ?? 'live') === 'live' && num(r.pnl_pct) !== null)
    .map((r) => ({
      exitReason: r.exit_reason || 'UNKNOWN',
      netPct: num(r.pnl_pct)!,
      detectToOpenMs: num(r.detect_to_open_ms),
      exitConfirmMs: num(r.exit_confirm_ms) ?? num(r.exit_trigger_to_confirm_ms),
      holdMs: num(r.hold_ms),
    }));
}

const f = (x: number, d = 1) => (Number.isFinite(x) ? `${x >= 0 ? '+' : '−'}${Math.abs(x).toFixed(d)}` : '—');
const ms = (xs: Array<number | null>) => {
  const v = xs.filter((x): x is number => x !== null && Number.isFinite(x));
  return v.length ? `${Math.round(median(v))} / ${Math.round(mean(v))}` : '—';
};

export function renderLiveVsPaper(live: readonly LiveTrade[], paper: readonly Trade[], paperRecostNetPct: (t: Trade) => number): string {
  const reasons = [...new Set([...live.map((t) => t.exitReason), ...paper.map((t) => t.exitReason)])].sort();
  const rows = reasons.map((r) => {
    const l = live.filter((t) => t.exitReason === r);
    const p = paper.filter((t) => t.exitReason === r);
    const lm = l.length ? mean(l.map((t) => t.netPct)) : NaN;
    const pm = p.length ? mean(p.map(paperRecostNetPct)) : NaN;
    return `| ${r} | ${l.length} | ${f(lm)} | ${p.length} | ${f(pm)} | ${f(lm - pm)} |`;
  });
  const lAll = mean(live.map((t) => t.netPct));
  const pAll = mean(paper.map(paperRecostNetPct));
  return [
    '## Live vs paper realization',
    '',
    `Live: ${live.length} trades (strategy week). Paper: ${paper.length} trades, re-costed at real fees. Both are **net %/trade**. The gap per exit reason is the fill and latency drag that fees alone do not explain.`,
    '',
    '| Exit reason | Live n | Live net % | Paper n | Paper net % (real fees) | Live − paper |',
    '|---|---|---|---|---|---|',
    ...rows,
    `| **All** | ${live.length} | ${f(lAll)} | ${paper.length} | ${f(pAll)} | **${f(lAll - pAll)}** |`,
    '',
    `Live latency, median / mean: detect→open ${ms(live.map((t) => t.detectToOpenMs))} ms · exit trigger→confirm ${ms(live.map((t) => t.exitConfirmMs))} ms.`,
    '',
  ].join('\n');
}
