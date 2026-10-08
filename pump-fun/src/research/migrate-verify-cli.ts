/**
 * Migration-racer verification CLI (spec §7). Read-only: RPC reads only.
 *
 *   npm run research:migrate-verify -- [--max-migrates 1000] [--probe-top 5]
 *     [--probe-depth 100] [--slot-window 150] [--out-json reports/...] [--out-md reports/...]
 *
 * RPC endpoint: --rpc, else HELIUS_RESEARCH_HTTP_URL, else HELIUS_HTTP_URL
 * (from .env — the RpcClient registers the URL as a secret for log redaction).
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { parseArgs } from 'node:util';
import { dirname, resolve } from 'node:path';
import { config as loadDotenv } from 'dotenv';
import { RpcClient } from '../core/rpc.ts';
import {
  probeCaller,
  rankCallers,
  renderMarkdown,
  sampleMigrates,
  type VerificationFindings,
} from './migrateVerify.ts';

loadDotenv();

const { values } = parseArgs({
  args: process.argv.slice(2),
  options: {
    rpc: { type: 'string' },
    'max-migrates': { type: 'string', default: '1000' },
    'probe-top': { type: 'string', default: '5' },
    'probe-depth': { type: 'string', default: '100' },
    'slot-window': { type: 'string', default: '150' },
    concurrency: { type: 'string', default: '4' },
    'max-rps': { type: 'string', default: '8' },
    'out-json': { type: 'string', default: 'reports/migrate-racer-verification.json' },
    'out-md': { type: 'string', default: 'reports/migrate-racer-verification.md' },
  },
});

const num = (v: string | undefined, name: string): number => {
  const n = Number(v);
  if (!Number.isInteger(n) || n < 0) {
    console.error(`invalid --${name}: ${v}`);
    process.exit(2);
  }
  return n;
};

const maxMigrates = num(values['max-migrates'], 'max-migrates');
const probeTop = num(values['probe-top'], 'probe-top');
const probeDepth = num(values['probe-depth'], 'probe-depth');
const slotWindow = num(values['slot-window'], 'slot-window');
const concurrency = num(values.concurrency, 'concurrency');
const maxRps = num(values['max-rps'], 'max-rps');

const rpcUrl = values.rpc ?? process.env.HELIUS_RESEARCH_HTTP_URL ?? process.env.HELIUS_HTTP_URL;
if (!rpcUrl) {
  console.error('no RPC endpoint: pass --rpc or set HELIUS_RESEARCH_HTTP_URL / HELIUS_HTTP_URL');
  process.exit(2);
}

const rpc = new RpcClient({ httpUrl: rpcUrl, maxConcurrent: concurrency, maxRps, timeoutMs: 15_000 });

const { samples, stats } = await sampleMigrates(rpc, {
  maxMigrates,
  concurrency,
  onProgress: (migrates, scanned) => console.error(`sampled ${migrates}/${maxMigrates} migrates (${scanned} scanned)`),
});
console.error(`sample complete: ${samples.length} migrates`);

const callersRanked = rankCallers(samples);
const probes = [];
for (const c of callersRanked.slice(0, probeTop)) {
  console.error(`probing ${c.caller} (${c.migrates} migrates)`);
  probes.push(
    await probeCaller(
      rpc,
      c.caller,
      samples.filter((s) => s.caller === c.caller),
      { depth: probeDepth, slotWindow, concurrency },
    ),
  );
}

const findings: VerificationFindings = {
  generatedAt: new Date().toISOString(),
  params: { maxMigrates, probeTop, probeDepth, slotWindow },
  stats,
  sampleCount: samples.length,
  callersRanked,
  probes,
};

for (const [path, content] of [
  [values['out-json']!, JSON.stringify(findings, null, 2)],
  [values['out-md']!, renderMarkdown(findings)],
] as const) {
  const abs = resolve(path);
  mkdirSync(dirname(abs), { recursive: true });
  writeFileSync(abs, content);
}
console.log(`wrote ${values['out-json']} and ${values['out-md']}`);
console.log(`migrates=${samples.length} callers=${callersRanked.length} probes=${probes.length}`);
