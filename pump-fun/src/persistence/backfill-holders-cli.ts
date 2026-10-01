/**
 * npm run db:backfill-holders
 *
 * One-off, idempotent: rewrites candidates/positions top10_share and
 * max_holder_share with H5's vault-excluded definition (see
 * backfillHolderShares.ts). Run where the DB lives (CONFIG_PATH selects it).
 */
import { loadConfig } from '../config/load.ts';
import { openDb } from './db.ts';
import { backfillHolderShares } from './backfillHolderShares.ts';

const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
const db = openDb({ path: config.persistence.dbPath });
const r = backfillHolderShares(db);
console.log(
  `holder shares backfilled: ${r.candidates} candidates (${r.nulled} without a pool -> NULL), ${r.positions} positions`,
);
db.close();
