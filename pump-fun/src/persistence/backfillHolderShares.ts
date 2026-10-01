import type { DB } from './db.ts';
import { effectiveHolderShares } from '../enrichment/holderShares.ts';

interface StoredEnrichment {
  pool?: { baseVault?: string; quoteVault?: string; baseMint?: string };
  holders?: { holders?: Array<{ account: string; owner?: string; share: number }> };
  mintInfo?: { isToken2022?: boolean };
}

export interface BackfillResult {
  candidates: number;
  positions: number;
  /** Rows with a holder snapshot but no decoded pool: set to NULL (raw was vault-dominated). */
  nulled: number;
}

/**
 * Rewrites candidates/positions top10_share + max_holder_share with H5's
 * definition (vaults, curve, burn excluded), recomputed from the stored
 * enrichment_json. Rows written before 2026-09-28 hold the RAW snapshot values.
 * Idempotent: re-running recomputes the same numbers. Positions take the
 * latest candidate row for the same mint — the same row they were created from.
 */
export function backfillHolderShares(db: DB): BackfillResult {
  const rows = db
    .prepare(`SELECT rowid AS id, mint, enrichment_json FROM candidates WHERE enrichment_json IS NOT NULL`)
    .all() as Array<{ id: number; mint: string; enrichment_json: string }>;
  const update = db.prepare(`UPDATE candidates SET top10_share = ?, max_holder_share = ? WHERE rowid = ?`);
  let candidates = 0;
  let nulled = 0;
  db.exec('BEGIN');
  try {
    for (const r of rows) {
      let e: StoredEnrichment;
      try {
        e = JSON.parse(r.enrichment_json) as StoredEnrichment;
      } catch {
        continue;
      }
      const list = e.holders?.holders;
      if (!Array.isArray(list)) continue;
      const pool = e.pool?.baseVault && e.pool.quoteVault ? { baseVault: e.pool.baseVault, quoteVault: e.pool.quoteVault } : undefined;
      const shares = effectiveHolderShares({ holders: list }, pool, e.pool?.baseMint ?? r.mint, e.mintInfo?.isToken2022 ?? false);
      update.run(shares?.top10Share ?? null, shares?.maxShare ?? null, r.id);
      candidates += 1;
      if (!shares) nulled += 1;
    }
    const pos = db
      .prepare(
        `UPDATE positions
            SET top10_share = (SELECT c.top10_share FROM candidates c WHERE c.mint = positions.mint ORDER BY c.rowid DESC LIMIT 1),
                max_holder_share = (SELECT c.max_holder_share FROM candidates c WHERE c.mint = positions.mint ORDER BY c.rowid DESC LIMIT 1)
          WHERE EXISTS (SELECT 1 FROM candidates c WHERE c.mint = positions.mint AND c.enrichment_json IS NOT NULL)`,
      )
      .run();
    db.exec('COMMIT');
    return { candidates, positions: Number(pos.changes), nulled };
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
}
