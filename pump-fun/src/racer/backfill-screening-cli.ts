/**
 * npm run racer:backfill-screening -- [--since ISO] [--limit N] [--dry] [--all]
 *
 * Stamps paper tracks with the guardrail-engine verdict at HEAD over their
 * synthesized candidates (racer/screening.ts). Local-only, no RPC: reserves
 * come from the track, the creator from the migration row. Idempotent:
 * screened tracks are skipped unless --all (re-run after an engine change —
 * see SCREENING_ENGINE).
 */
import { loadConfig, ConfigError } from '../config/load.ts';
import { openDb } from '../persistence/db.ts';
import { Repositories } from '../persistence/repositories.ts';
import { PROGRAM_IDS } from '../core/constants.ts';
import { screenRacerTrack } from './screening.ts';

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

interface TrackRow {
  mint: string;
  pool_address: string | null;
  reach_quote_sol: number;
  entry_features_json: string | null;
  migrate_slot: number | null;
  coin_creator: string | null;
}

async function main(): Promise<void> {
  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  const since = arg('--since') ?? new Date(Date.now() - 30 * 24 * 3_600_000).toISOString();
  const limit = Math.max(1, Number(arg('--limit') ?? 1000) || 1000);
  const dry = process.argv.includes('--dry');
  const all = process.argv.includes('--all');

  const db = openDb({ path: config.persistence.dbPath });
  const repos = new Repositories(db);
  const sinceSql = since.slice(0, 19).replace('T', ' ');

  const tracks = db
    .prepare(
      `SELECT p.mint, p.pool_address, p.reach_quote_sol, p.entry_features_json, m.migrate_slot, m.coin_creator
         FROM racer_paper_tracks p LEFT JOIN racer_migrations m ON m.mint = p.mint
        WHERE p.created_at >= ? ${all ? '' : 'AND p.screening_json IS NULL '}ORDER BY p.rowid LIMIT ?`,
    )
    .all(sinceSql, limit) as unknown as TrackRow[];

  let screened = 0;
  let unknown = 0;
  let failed = 0;
  for (const t of tracks) {
    let features: { quoteVaultSol?: number | null; tokenProgram?: string | null;
      transferFee?: { bps: number; maxRaw: string } | 'unknown' | null } = {};
    try {
      features = JSON.parse(t.entry_features_json ?? '{}') as typeof features;
    } catch {
      features = {};
    }
    try {
      const verdict = screenRacerTrack(
        {
          mint: t.mint, poolAddress: t.pool_address ?? '', migrateSlot: t.migrate_slot,
          reachQuoteSol: t.reach_quote_sol, quoteVaultSol: features.quoteVaultSol ?? null,
          coinCreator: t.coin_creator ?? '', isToken2022: features.tokenProgram === PROGRAM_IDS.TOKEN_2022,
          transferFee: features.transferFee,
        },
        config, repos);
      if (!verdict) {
        unknown++;
        continue;
      }
      if (!dry) repos.updateRacerPaperScreening(t.mint, JSON.stringify(verdict));
      screened++;
    } catch {
      failed++;
    }
  }
  console.log(JSON.stringify({ dry, candidates: tracks.length, screened, unknown, failed }));
  if (!all && unknown > 0) {
    console.log('note: unknown = tracks that predate entry_features (no vault read for H7); left unscreened');
  }
}

main().catch((err) => {
  if (err instanceof ConfigError) {
    console.error(err.message);
    process.exit(1);
  }
  throw err;
});
