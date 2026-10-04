import { mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { loadConfig } from '../config/load.ts';
import { RpcClient, type ParsedTx } from '../core/rpc.ts';
import { installKeepAliveDispatcher } from '../core/http.ts';
import { openDb } from '../persistence/db.ts';
import { exitCfgFor } from '../exits/engine.ts';
import { revivePoolPricing } from '../positions/pricing.ts';
import { buildPricePath, fetchPoolHistory, readPoolVirtualQuote, simulateTrade, type ReplayOutcome } from './poolReplay.ts';

/**
 * npm run research:pool-replay -- [--days 7] [--max-tx 400] [--sizes 0.0067,0.02,0.05]
 *                                 [--fee-bps 125] [--tx-cost 0.0000725] [--horizon-min 10]
 *
 * For every live position in the window: rebuild the pool's first --max-tx
 * transactions from chain (cached under data/replay-cache), price them on
 * vault + virtual reserve, and run the CURRENT config's exits from
 *   (a) where our buy actually landed, and
 *   (b) the pool's first swap (the latency counterfactual).
 * Writes reports/pool-replay-<ts>.{json,md}. Reads only; sends nothing.
 */

function arg(name: string, fallback: string): string {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1]! : fallback;
}

async function main(): Promise<void> {
  installKeepAliveDispatcher();
  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  const days = Number(arg('days', '7'));
  const maxTx = Number(arg('max-tx', '400'));
  const sizes = arg('sizes', '0.0067,0.02,0.05').split(',').map(Number);
  const cfg = {
    exits: exitCfgFor(config, false),
    feeBps: Number(arg('fee-bps', '125')),
    txCostSol: Number(arg('tx-cost', '0.0000725')),
    horizonMs: Number(arg('horizon-min', String(config.exits.timeStopMinutes))) * 60_000,
  };
  const httpUrl = config.rpc?.enrichmentHttp?.trim() || config.rpc!.primaryHttp;
  const rpc = new RpcClient({ httpUrl, maxConcurrent: 8, timeoutMs: 5_000, maxRps: Number(arg('rps', String(config.rpc?.researchMaxRps || 30))) });
  const db = openDb({ path: config.persistence.dbPath, busyTimeoutMs: 2_000 });

  const rows = db
    .prepare(
      `SELECT mint, size_sol AS sizeSol, entry_tx AS entryTx, pricing_json AS pricingJson,
              COALESCE(wallet_pnl_sol, net_pnl_sol) AS actualNetSol, exit_reason AS exitReason
         FROM positions
        WHERE rowid IN (SELECT MAX(rowid) FROM positions GROUP BY mint)
          AND mode = 'live' AND state = 'CLOSED' AND pricing_json IS NOT NULL
          AND julianday(COALESCE(closed_at, created_at)) >= julianday('now', ?)`,
    )
    .all(`-${days} days`) as Array<{ mint: string; sizeSol: number; entryTx: string | null; pricingJson: string; actualNetSol: number | null; exitReason: string | null }>;

  const cacheDir = join(config.persistence.dbPath, '..', 'replay-cache');
  mkdirSync(cacheDir, { recursive: true });
  const results: Array<Record<string, unknown>> = [];
  for (const [n, r] of rows.entries()) {
    const { pricing } = revivePoolPricing(r.pricingJson);
    const cacheFile = join(cacheDir, `${pricing.poolAddress}.json`);
    let txs: ParsedTx[];
    if (existsSync(cacheFile)) {
      txs = JSON.parse(readFileSync(cacheFile, 'utf8')) as ParsedTx[];
    } else {
      txs = await fetchPoolHistory(rpc, pricing.poolAddress, { maxTx });
      writeFileSync(cacheFile, JSON.stringify(txs));
    }
    const virtual = await readPoolVirtualQuote(rpc, pricing.poolAddress).catch(() => 0n);
    const path = buildPricePath(txs, pricing.poolAddress, pricing.baseMint, virtual, pricing.baseDecimals);
    const ourIdx = r.entryTx ? path.findIndex((p) => p.signature === r.entryTx) : -1;
    const firstSwap = path.findIndex((p, i) => i > 0 && !p.err);
    const row: Record<string, unknown> = {
      mint: r.mint,
      pool: pricing.poolAddress,
      points: path.length,
      ourIndex: ourIdx,
      ourSlotOffset: ourIdx >= 0 ? path[ourIdx]!.slot - path[0]!.slot : null,
      actualNetSol: r.actualNetSol,
      actualExit: r.exitReason,
    };
    for (const size of sizes) {
      const at = (idx: number): ReplayOutcome | null => (idx >= 0 ? simulateTrade(path, idx, size, cfg, virtual) : null);
      row[`ours_${size}`] = at(ourIdx);
      row[`first_${size}`] = at(firstSwap);
    }
    results.push(row);
    process.stderr.write(`\r${n + 1}/${rows.length} ${r.mint.slice(0, 6)} points=${path.length}   `);
  }
  process.stderr.write('\n');

  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  mkdirSync('reports', { recursive: true });
  writeFileSync(`reports/pool-replay-${stamp}.json`, JSON.stringify({ cfg: { ...cfg, exits: undefined }, exits: cfg.exits, results }, null, 2));
  const md = renderReplayMarkdown(results, sizes, cfg);
  writeFileSync(`reports/pool-replay-${stamp}.md`, md);
  console.log(md); // eslint-disable-line no-console
}

export function renderReplayMarkdown(results: Array<Record<string, unknown>>, sizes: number[], cfg: { feeBps: number; txCostSol: number; horizonMs: number }): string {
  const lines = [
    `# Pool replay — ${results.length} live trades`,
    '',
    `Exits: current config. Fees ${cfg.feeBps} bps/side, ${cfg.txCostSol} SOL/tx, horizon ${cfg.horizonMs / 60_000} min. Prices on vault + virtual reserve.`,
    '',
    '| entry | size SOL | n | win % | mean net % | median net % | total net SOL |',
    '|---|---|---|---|---|---|---|',
  ];
  for (const kind of ['ours', 'first']) {
    for (const size of sizes) {
      const outs = results.map((r) => r[`${kind}_${size}`] as ReplayOutcome | null).filter((o): o is ReplayOutcome => !!o && o.exitReason !== 'NO_DATA');
      if (!outs.length) continue;
      const pcts = outs.map((o) => o.netPct).sort((a, b) => a - b);
      const mean = pcts.reduce((s, x) => s + x, 0) / pcts.length;
      const median = pcts[Math.floor(pcts.length / 2)]!;
      const wins = outs.filter((o) => o.netSol > 0).length;
      const total = outs.reduce((s, o) => s + o.netSol, 0);
      lines.push(`| ${kind === 'ours' ? 'our landed position' : 'first swap'} | ${size} | ${outs.length} | ${((wins / outs.length) * 100).toFixed(0)} | ${mean.toFixed(2)} | ${median.toFixed(2)} | ${total.toFixed(4)} |`);
    }
  }
  const actual = results.reduce((s, r) => s + (Number(r.actualNetSol) || 0), 0);
  lines.push('', `Actual wallet net over the same trades: ${actual.toFixed(4)} SOL.`);
  const offsets = results.map((r) => r.ourSlotOffset).filter((x): x is number => typeof x === 'number');
  if (offsets.length) {
    const hist = new Map<number, number>();
    for (const o of offsets) hist.set(o, (hist.get(o) ?? 0) + 1);
    lines.push('', `Our buy's slot after pool creation: ${[...hist.entries()].sort((a, b) => a[0] - b[0]).map(([k, v]) => `+${k}: ${v}`).join(', ')}`);
  }
  return `${lines.join('\n')}\n`;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    console.error(err); // eslint-disable-line no-console
    process.exit(1);
  });
}
