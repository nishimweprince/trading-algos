import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';
import type { Candidate } from '../../enrichment/types.ts';
import type { Config } from '../../config/schema.ts';
import type { Repositories } from '../../persistence/repositories.ts';
import { quoteReserveSol } from '../../enrichment/pool.ts';
import type { ManipulationFeatures } from '../../enrichment/features/types.ts';

/** Solana slot time used to turn a slot gap into an age. */
export const MS_PER_SLOT = 400;

/**
 * H12 — canonical graduation population (work plan 2026-09-25 P2.2, F10).
 *
 * The 7-day review split cleanly by population: non-`pump`-suffix mints were
 * 26 % of trades at WR 37.5 % / −5.9 %/trade, matching veto-review segments
 * B/C (tokens created 1–2 s before their MigrateV2, bundle-and-dump). Only
 * segment A — a `pump` mint that lived on its curve for a while and migrated
 * with a normal-sized pool — is traded:
 *
 *  - mint ends in `pump` (vanity-grind of the pump.fun frontend);
 *  - mint age at migration >= minMintAgeMs;
 *  - pool SOL within [minPoolSol, maxPoolSol].
 *
 * An unknown mint age is a hard FAIL by default (`unknownAgePolicy: veto`):
 * an age the indexers cannot see yet is itself the insta-graduation
 * signature. It must be `fail`, not `unknown` — paper/dry-run never veto on
 * unknowns, and the filter has to hold in every mode.
 */
export function checkPopulation(ctx: CheckContext): CheckResult {
  const id = 'H12';
  const label = 'Canonical graduation population';
  const p = ctx.config.guardrails.population;
  if (!p.enabled) return { id, label, status: 'pass', detail: 'population filter disabled' };

  const pre = populationPrecheck(ctx.candidate, ctx.config);
  if (pre) return pre;

  const c = ctx.candidate;
  const poolSol = quoteReserveSol(c.enrichment.pool!);
  const age = mintAgeAtMigration(c, ctx.repos);
  if (age?.lowerBound) {
    // Older than the scanned history: proves the floor. A short bound proves
    // nothing (a busy curve), so it falls through to the unknown policy.
    if (age.ms >= p.minMintAgeMs) {
      return {
        id,
        label,
        status: 'pass',
        detail: `pump mint, >= ${(age.ms / 1000).toFixed(0)} s old (${age.source}), pool ${poolSol.toFixed(1)} SOL`,
      };
    }
  } else if (age !== null && age.ms < p.minMintAgeMs) {
    return {
      id,
      label,
      status: 'fail',
      reason: 'insta_graduation',
      detail: `mint ${(age.ms / 1000).toFixed(1)} s old at migration (< ${p.minMintAgeMs / 1000} s, ${age.source})`,
    };
  }
  if (age === null || age.lowerBound) {
    return p.unknownAgePolicy === 'allow'
      ? { id, label, status: 'pass', detail: `mint age unknown (allowed); pool ${poolSol.toFixed(1)} SOL` }
      : { id, label, status: 'fail', reason: 'mint_age_unknown', detail: 'mint age at migration unknown' };
  }
  return {
    id,
    label,
    status: 'pass',
    detail: `pump mint, ${(age.ms / 1000).toFixed(0)} s old (${age.source}), pool ${poolSol.toFixed(1)} SOL`,
  };
}

/**
 * The half of H12 that needs only the mint and the pool snapshot — suffix,
 * pool present, pool SOL band — in H12's order and with its exact result.
 * Returns the H12 FAIL, or null when this half passes (or H12 is disabled)
 * and the mint-age half still has to decide. Screening runs it right after
 * enrichment to skip the H4 probe and features for a candidate H12 will
 * veto anyway (population.earlyVeto).
 */
export function populationPrecheck(c: Candidate, config: Config): CheckResult | null {
  const id = 'H12';
  const label = 'Canonical graduation population';
  const p = config.guardrails.population;
  if (!p.enabled) return null;
  if (p.requirePumpSuffix && !c.graduation.mint.endsWith('pump')) {
    return { id, label, status: 'fail', reason: 'non_pump_suffix', detail: 'mint does not end in `pump`' };
  }
  const pool = c.enrichment.pool;
  if (!pool) return { id, label, status: 'fail', reason: 'no_pool', detail: 'pool not decoded — pool SOL unknown' };
  const poolSol = quoteReserveSol(pool);
  if (poolSol < p.minPoolSol || poolSol > p.maxPoolSol) {
    return {
      id,
      label,
      status: 'fail',
      reason: 'pool_sol_out_of_band',
      detail: `pool ${poolSol.toFixed(1)} SOL outside [${p.minPoolSol}, ${p.maxPoolSol}]`,
    };
  }
  return null;
}

/**
 * population.earlyVeto: when H12's suffix / pool half already fails, mark the
 * candidate so H4 and H13 report `skipped_early_veto` instead of reading
 * inputs screening did not fetch. Returns that H12 failure, or null (flag off,
 * or the precheck passes and screening runs in full).
 */
export function markEarlyVeto(c: Candidate, config: Config): CheckResult | null {
  if (!config.guardrails.population.earlyVeto) return null;
  const pre = populationPrecheck(c, config);
  if (pre) c.enrichment.earlyVeto = { code: 'H12', reason: pre.reason ?? 'population' };
  return pre;
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
