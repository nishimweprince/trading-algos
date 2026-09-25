/**
 * npm run research:recost -- --csv <trades.csv> [--out report.md] [--seed 1]
 *   [--logged-swap-fee-pct 0.25] [--title "..."] [--preamble file.md]
 *   [--live strategy-week-trades.csv] [--postscript file.md]
 *
 * Re-costs a trade-blotter export at the real PumpSwap fee schedule and
 * writes a markdown report (stdout when --out is omitted). Deterministic for
 * a given input and seed.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { parseArgs } from 'node:util';
import { loadTrades, recostTrade } from './trades.ts';
import { renderRecostReport, sha256 } from './recost.ts';
import { loadLiveTrades, renderLiveVsPaper } from './liveVsPaper.ts';

const { values } = parseArgs({
  options: {
    csv: { type: 'string' },
    out: { type: 'string' },
    seed: { type: 'string', default: '1' },
    'logged-swap-fee-pct': { type: 'string', default: '0.25' },
    title: { type: 'string', default: 'Re-cost report' },
    preamble: { type: 'string' },
    // Live strategy-week trades CSV: appends a live-vs-paper realization section.
    live: { type: 'string' },
    postscript: { type: 'string' },
  },
});

if (!values.csv) {
  console.error('usage: research:recost -- --csv <trades.csv> [--out report.md] [--seed 1]');
  process.exit(2);
}

const text = readFileSync(values.csv, 'utf8');
const trades = loadTrades(text);
let report = renderRecostReport(trades, {
  title: values.title!,
  source: values.csv,
  sourceSha256: sha256(text),
  seed: Number(values.seed),
  loggedSwapFeePct: Number(values['logged-swap-fee-pct']),
  ...(values.preamble ? { preamble: readFileSync(values.preamble, 'utf8').trim() } : {}),
});
if (values.live) {
  const logged = Number(values['logged-swap-fee-pct']);
  report += `\n${renderLiveVsPaper(loadLiveTrades(readFileSync(values.live, 'utf8')), trades, (t) => (recostTrade(t, { loggedSwapFeePct: logged }).netPnlSol / t.sizeSol) * 100)}`;
  report += `\nLive source: \`${values.live}\` · sha256 \`${sha256(readFileSync(values.live, 'utf8'))}\`\n`;
}
if (values.postscript) report += `\n${readFileSync(values.postscript, 'utf8').trim()}\n`;

if (values.out) {
  writeFileSync(values.out, report);
  console.log(`wrote ${values.out}`);
} else {
  process.stdout.write(report);
}
