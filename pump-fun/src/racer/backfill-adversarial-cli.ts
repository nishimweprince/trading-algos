/**
 * npm run racer:backfill-adversarial -- [--since ISO] [--limit N] [--dry] [--phase features|fees|both]
 *
 * Backfills the adversarial-leg inputs (racer/adversarial.ts) into
 * entry_features_json for tracks that predate them:
 *
 * - features (local, no RPC): bundledTokens from the migration row and
 *   poolCanonical derived offline (canonical PDA vs stored pool address).
 * - fees (RPC): the Token-2022 transfer fee for T22 tracks whose mint was
 *   never read — one getAccountInfo per mint, decoded, merged.
 *
 * Idempotent: tracks already carrying the inputs are skipped. Additive
 * merges only — existing keys are never rewritten.
 */
import { loadConfig, ConfigError } from '../config/load.ts';
import { openDb } from '../persistence/db.ts';
import { Repositories } from '../persistence/repositories.ts';
import { RpcClient } from '../core/rpc.ts';
import { PROGRAM_IDS } from '../core/constants.ts';
import { decodeTransferFee } from '../enrichment/mint.ts';
import { isCanonicalPool } from './adversarial.ts';

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

type Features = Record<string, unknown>;

interface TrackRow {
  mint: string;
  pool_address: string | null;
  entry_features_json: string | null;
  bundled_tokens: string | null;
}

function parseFeatures(json: string | null): Features | null {
  if (!json) return {};
  try {
    const v = JSON.parse(json) as unknown;
    return typeof v === 'object' && v !== null ? (v as Features) : null;
  } catch {
    return null;
  }
}

async function main(): Promise<void> {
  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  const since = arg('--since') ?? new Date(Date.now() - 30 * 24 * 3_600_000).toISOString();
  const limit = Math.max(1, Number(arg('--limit') ?? 1000) || 1000);
  const dry = process.argv.includes('--dry');
  const phase = arg('--phase') ?? 'both';
  if (phase !== 'features' && phase !== 'fees' && phase !== 'both') throw new ConfigError('--phase must be features|fees|both');

  const db = openDb({ path: config.persistence.dbPath });
  const repos = new Repositories(db);
  const sinceSql = since.slice(0, 19).replace('T', ' ');
  const tracks = db
    .prepare(
      `SELECT p.mint, p.pool_address, p.entry_features_json, m.bundled_tokens
         FROM racer_paper_tracks p LEFT JOIN racer_migrations m ON m.mint = p.mint
        WHERE p.created_at >= ? ORDER BY p.rowid LIMIT ?`,
    )
    .all(sinceSql, limit) as unknown as TrackRow[];

  if (phase === 'features' || phase === 'both') {
    let merged = 0;
    let skipped = 0;
    for (const t of tracks) {
      const f = parseFeatures(t.entry_features_json);
      if (!f) {
        skipped++;
        continue;
      }
      let dirty = false;
      if ((f.bundledTokens === undefined || f.bundledTokens === null) && t.bundled_tokens !== null) {
        f.bundledTokens = t.bundled_tokens;
        dirty = true;
      }
      if (f.poolCanonical === undefined || f.poolCanonical === null) {
        const canonical = isCanonicalPool(t.mint, t.pool_address);
        if (canonical !== null) {
          f.poolCanonical = canonical;
          dirty = true;
        }
      }
      if (!dirty) {
        skipped++;
        continue;
      }
      if (!dry) repos.updateRacerPaperEntryFeatures(t.mint, JSON.stringify(f));
      merged++;
    }
    console.log(JSON.stringify({ phase: 'features', dry, candidates: tracks.length, merged, skipped }));
  }

  if (phase === 'fees' || phase === 'both') {
    if (!config.rpc?.primaryHttp) throw new ConfigError('rpc.primaryHttp is required for --phase fees');
    const rpc = new RpcClient({ httpUrl: config.rpc.primaryHttp, fallbackHttpUrls: config.rpc.fallbackHttp, maxConcurrent: 4, timeoutMs: 10_000 });
    let merged = 0;
    let skipped = 0;
    let failed = 0;
    for (const t of tracks) {
      const f = parseFeatures(t.entry_features_json);
      if (!f || f.tokenProgram !== PROGRAM_IDS.TOKEN_2022) {
        skipped++;
        continue;
      }
      // null = proven clean; an object = already read. Only absent/'unknown' need the RPC.
      if (f.transferFee !== undefined && f.transferFee !== 'unknown') {
        skipped++;
        continue;
      }
      try {
        const acct = await rpc.getAccountInfoBase64(t.mint, 'confirmed').catch(() => null);
        const fee = acct ? decodeTransferFee(acct.data, acct.owner) : null;
        f.transferFee = fee ? { bps: fee.bps, maxRaw: fee.maxRaw.toString() } : (acct ? null : 'unknown');
        if (!dry) repos.updateRacerPaperEntryFeatures(t.mint, JSON.stringify(f));
        merged++;
      } catch {
        failed++;
      }
    }
    console.log(JSON.stringify({ phase: 'fees', dry, candidates: tracks.length, merged, skipped, failed }));
  }
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
