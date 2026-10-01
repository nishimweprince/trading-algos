import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';
import { RUG_EXTENSION_IDS, RUG_EXTENSION_NAMES } from '../../enrichment/mint.ts';

/**
 * P0 — canonical pump.fun migration. Replaces H1 (mint authority), H2
 * (freeze authority), H3 (LP burned), H9 (Token-2022 traps) and H11
 * (indexed mint), which all asked one question: "did pump.fun make this?"
 *
 * The pool is read at pump.fun's canonical PumpSwap PDA (fastRead.ts). Only
 * the pump.fun program's pool-authority can create that account, and it only
 * does so in Migrate, for a mint the pump.fun program created — which is
 * minted with no freeze authority, has its mint authority revoked, carries no
 * transfer hook / fee / delegate extensions, and has its LP burned at
 * migration. So a decoded pool at the canonical PDA with a coin_creator set is
 * the proof; the mint's authorities and extensions come back in the same
 * read and are re-checked for free (a failure there means the premise broke,
 * and the answer is no).
 *
 * 2026-09-28 data: every H1 / H2 / H3 / H9 failure (8 mints) was a
 * non-canonical, non-`pump` mint; H11 vetoed 33 mints purely on indexer lag.
 */
export function checkProvenance(ctx: CheckContext): CheckResult {
  const id = 'P0';
  const label = 'Canonical pump.fun migration';
  const { pool, mintInfo } = ctx.candidate.enrichment;
  const fail = (reason: string, detail: string): CheckResult => ({ id, label, status: 'fail', reason, detail });

  if (!pool) {
    const miss = ctx.candidate.enrichment.unknowns.find((u) => u.startsWith('pool:'));
    return fail(miss?.slice(5) ?? 'pool_not_found', 'no PumpSwap pool at the canonical pump.fun PDA');
  }
  if (!pool.isCanonical) return fail('non_canonical', 'pool has no coin_creator (not a pump.fun migration)');
  if (!mintInfo) return fail('mint_unreadable', 'mint account missing from the pool read');
  if (mintInfo.mintAuthority !== null) return fail('mint_authority', `mint authority active: ${mintInfo.mintAuthority}`);
  if (mintInfo.freezeAuthority !== null) return fail('freeze_authority', `freeze authority active: ${mintInfo.freezeAuthority}`);
  const bad = mintInfo.extensions.filter((e) => RUG_EXTENSION_IDS.has(e));
  if (bad.length > 0) {
    return fail('rug_extension', `rug extensions: ${bad.map((e) => RUG_EXTENSION_NAMES[e] ?? e).join(', ')}`);
  }
  return {
    id,
    label,
    status: 'pass',
    detail: `canonical pool, authorities revoked, ${mintInfo.isToken2022 ? 'Token-2022 (no rug extensions)' : 'SPL mint'}`,
  };
}
