import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';

/**
 * H11 — mint indexed (not a same-slot bundled launch).
 *
 * 2026-09-18 review: 47% of live candidates were tokens created 1–2 s before
 * their MigrateV2 (create → fill the curve → migrate inside a bundle). At
 * enrichment nothing has indexed them yet — the mint read, DAS metadata and
 * the third-party token-age API all miss at once while the pool PDA (read on
 * the same RPC) is present. Sampled outcomes: the ~67 SOL ones went to x0.01
 * (bundle-and-dump); the 200–5,000 SOL ones were un-scalpable or drained.
 *
 * Until now they died under the wrong label (LOW_SCORE with the score pinned
 * at baseline 40, or UNKNOWN:H1/H2/H9). This makes it a named hard veto so a
 * future mint-read retry cannot let them through at score 65. Requiring ALL
 * three misses keeps a partial RPC hiccup (mint 429'd, DAS fine) from tripping
 * it; a full RPC outage already vetoes via the missing pool.
 */
export function checkIndexed(ctx: CheckContext): CheckResult {
  const id = 'H11';
  const label = 'Mint indexed (not a same-slot launch)';
  const e = ctx.candidate.enrichment;
  if (!e.pool) return { id, label, status: 'pass', detail: 'no pool snapshot — nothing to compare' };
  const ageMissing = ctx.config.guardrails.tokenAgeEnabled ? e.tokenAgeMs === undefined : true;
  if (e.mintInfo === undefined && e.metadata === undefined && ageMissing) {
    return {
      id,
      label,
      status: 'fail',
      reason: 'unindexed_mint',
      detail: 'mint account, DAS metadata and token age all unindexed at graduation — same-slot bundled launch',
    };
  }
  return { id, label, status: 'pass', detail: 'mint indexed' };
}
