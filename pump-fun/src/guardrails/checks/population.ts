import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';
import type { Candidate } from '../../enrichment/types.ts';
import type { Repositories } from '../../persistence/repositories.ts';
import { quoteReserveSol } from '../../enrichment/pool.ts';

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

  const c = ctx.candidate;
  if (p.requirePumpSuffix && !c.graduation.mint.endsWith('pump')) {
    return { id, label, status: 'fail', reason: 'non_pump_suffix', detail: 'mint does not end in `pump`' };
  }

  const pool = c.enrichment.pool;
  if (!pool) return { id, label, status: 'unknown', reason: 'no_pool', detail: 'pool not decoded — pool SOL unknown' };
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

  const age = mintAgeAtMigration(c, ctx.repos);
  if (age === null) {
    return p.unknownAgePolicy === 'allow'
      ? { id, label, status: 'pass', detail: `mint age unknown (allowed); pool ${poolSol.toFixed(1)} SOL` }
      : { id, label, status: 'fail', reason: 'mint_age_unknown', detail: 'mint age at migration unknown' };
  }
  if (age.ms < p.minMintAgeMs) {
    return {
      id,
      label,
      status: 'fail',
      reason: 'insta_graduation',
      detail: `mint ${(age.ms / 1000).toFixed(1)} s old at migration (< ${p.minMintAgeMs / 1000} s, ${age.source})`,
    };
  }
  return {
    id,
    label,
    status: 'pass',
    detail: `pump mint, ${(age.ms / 1000).toFixed(0)} s old (${age.source}), pool ${poolSol.toFixed(1)} SOL`,
  };
}

/**
 * Mint age at migration, best source first:
 *   1. launch slot vs migration slot (on-chain, needs the launch feed to have seen it);
 *   2. launch row insert time vs detection wall-clock (second resolution);
 *   3. enrichment.tokenAgeMs (pump.fun coin API, advisory).
 */
export function mintAgeAtMigration(
  c: Candidate,
  repos: Pick<Repositories, 'launchByMint'>,
): { ms: number; source: 'slot' | 'launch_clock' | 'token_age_api' } | null {
  let launch: ReturnType<Repositories['launchByMint']> = null;
  try {
    launch = repos.launchByMint(c.graduation.mint);
  } catch {
    launch = null;
  }
  if (launch?.slot && c.graduation.slot > 0 && c.graduation.slot >= launch.slot) {
    return { ms: (c.graduation.slot - launch.slot) * MS_PER_SLOT, source: 'slot' };
  }
  if (launch?.createdAtMs && c.graduation.detectedAtMs !== undefined && c.graduation.detectedAtMs >= launch.createdAtMs - 1_000) {
    return { ms: Math.max(0, c.graduation.detectedAtMs - launch.createdAtMs), source: 'launch_clock' };
  }
  if (typeof c.enrichment.tokenAgeMs === 'number' && Number.isFinite(c.enrichment.tokenAgeMs)) {
    return { ms: Math.max(0, c.enrichment.tokenAgeMs), source: 'token_age_api' };
  }
  return null;
}
