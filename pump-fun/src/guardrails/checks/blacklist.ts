import { existsSync } from 'node:fs';
import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';

/**
 * H8 (serial rugger). Now that the pool decoder identifies the dev
 * (coin_creator), both the mint and the creator are checked against the local
 * blacklist. The launch-history heuristic (creator's recent tokens that went to
 * zero) is a future enhancement — the blacklist is the enforced clause, fed by
 * the auto-blacklist-on-rug logic (Section 6.5, Phase 5).
 */
export function checkSerialRugger(ctx: CheckContext): CheckResult {
  const mint = ctx.candidate.graduation.mint;
  const creator = ctx.candidate.enrichment.pool?.coinCreator;

  if (ctx.repos.isMintBlacklisted(mint)) {
    return { id: 'H8', label: 'Not a serial rugger', status: 'fail', detail: 'mint is blacklisted' };
  }
  if (creator && ctx.repos.isCreatorBlacklisted(creator)) {
    return { id: 'H8', label: 'Not a serial rugger', status: 'fail', detail: `creator ${creator} is blacklisted` };
  }
  return { id: 'H8', label: 'Not a serial rugger', status: 'pass', detail: 'mint + creator not blacklisted' };
}

/**
 * H10 — circuit breakers clear. The full risk manager (Section 8) lands in
 * Phase 5; today this enforces the file-sentinel kill switch: presence of a
 * `KILL` file blocks all entries.
 */
export function checkBreakers(ctx: CheckContext): CheckResult {
  // File sentinel — belt and suspenders even when the risk manager is present.
  if (existsSync('KILL')) {
    return { id: 'H10', label: 'Circuit breakers clear', status: 'fail', detail: 'KILL sentinel present' };
  }
  // Full risk-manager consult (daily loss, consecutive losses, emergency count,
  // wallet floor, stream-down, kill latch).
  if (ctx.risk) {
    const decision = ctx.risk.canEnter();
    if (!decision.ok) {
      return { id: 'H10', label: 'Circuit breakers clear', status: 'fail', detail: `${decision.reason}: ${decision.detail ?? ''}` };
    }
  }
  return { id: 'H10', label: 'Circuit breakers clear', status: 'pass' };
}
