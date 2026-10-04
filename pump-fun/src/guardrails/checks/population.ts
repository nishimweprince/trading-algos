import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';
import type { Candidate } from '../../enrichment/types.ts';
import type { Repositories } from '../../persistence/repositories.ts';
import type { ManipulationFeatures } from '../../enrichment/features/types.ts';

/** Solana slot time used to turn a slot gap into an age. */
export const MS_PER_SLOT = 400;

/**
 * H12 — canonical graduation population, now just the mint suffix.
 *
 * Non-`pump`-suffix mints were 26 % of the 2026-09 trades at WR 37.5 % /
 * −5.9 %/trade (bundle-and-dump segments B/C), so they stay out. The mint-age
 * (insta-graduation) and pool-SOL band rules were removed 2026-10-04; H7
 * still floors empty or lagging pools. `mintAgeAtMigration` below stays for
 * the research features.
 */
export function checkPopulation(ctx: CheckContext): CheckResult {
  const id = 'H12';
  const label = 'Canonical graduation population';
  const p = ctx.config.guardrails.population;
  if (!p.enabled) return { id, label, status: 'pass', detail: 'population filter disabled' };
  if (p.requirePumpSuffix && !ctx.candidate.graduation.mint.endsWith('pump')) {
    return { id, label, status: 'fail', reason: 'non_pump_suffix', detail: 'mint does not end in `pump`' };
  }
  return { id, label, status: 'pass', detail: p.requirePumpSuffix ? 'pump mint' : 'suffix not required' };
}

export type MintAgeSource = 'curve_slot' | 'slot' | 'launch_clock' | 'curve_lower_bound';

/**
 * Mint age at migration, best source first:
 *   1. bonding-curve creation slot vs migration slot (on-chain; the P3.3 curve
 *      scan reached the curve's first tx);
 *   2. launch slot vs migration slot (needs the launch feed to have seen it);
 *   3. launch row insert time vs detection wall-clock (second resolution);
 *   4. oldest curve slot scanned vs migration slot — a LOWER BOUND
 *      (`lowerBound: true`) when the scan hit its page cap or budget; this is
 *      what covers mints created before the process started (no launch row).
 *
 * `features` defaults to the candidate's; FeatureEngine passes its own
 * in-progress object before attaching it.
 */
export function mintAgeAtMigration(
  c: Candidate,
  repos: Pick<Repositories, 'launchByMint'>,
  features: ManipulationFeatures | undefined = c.enrichment.features,
): { ms: number; source: MintAgeSource; lowerBound?: true } | null {
  const gradSlot = c.graduation.slot;
  const curve = features?.curve;
  if (curve?.creationSlot && gradSlot > 0 && gradSlot >= curve.creationSlot) {
    return { ms: (gradSlot - curve.creationSlot) * MS_PER_SLOT, source: 'curve_slot' };
  }
  let launch: ReturnType<Repositories['launchByMint']> = null;
  try {
    launch = repos.launchByMint(c.graduation.mint);
  } catch {
    launch = null;
  }
  if (launch?.slot && gradSlot > 0 && gradSlot >= launch.slot) {
    return { ms: (gradSlot - launch.slot) * MS_PER_SLOT, source: 'slot' };
  }
  if (launch?.createdAtMs && c.graduation.detectedAtMs !== undefined && c.graduation.detectedAtMs >= launch.createdAtMs - 1_000) {
    return { ms: Math.max(0, c.graduation.detectedAtMs - launch.createdAtMs), source: 'launch_clock' };
  }
  if (curve?.oldestSlotScanned && gradSlot > 0 && gradSlot >= curve.oldestSlotScanned) {
    return { ms: (gradSlot - curve.oldestSlotScanned) * MS_PER_SLOT, source: 'curve_lower_bound', lowerBound: true };
  }
  return null;
}
