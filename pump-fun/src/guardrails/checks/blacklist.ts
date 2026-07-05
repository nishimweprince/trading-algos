import { existsSync } from 'node:fs';
import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';

/**
 * H8 (serial rugger) — blacklist portion is live now: a mint on the local
 * blacklist is an immediate veto. The launch-history heuristic (creator's
 * recent tokens that went to zero) needs the creator wallet, which comes from
 * the pool/bonding-curve decode (pending — see pending.ts), so that portion is
 * reported unknown until then.
 */
export function checkSerialRugger(ctx: CheckContext): CheckResult {
  const mint = ctx.candidate.graduation.mint;
  if (ctx.repos.isMintBlacklisted(mint)) {
    return { id: 'H8', label: 'Not a serial rugger', status: 'fail', detail: 'mint is blacklisted' };
  }
  return {
    id: 'H8',
    label: 'Not a serial rugger',
    status: 'unknown',
    detail: 'creator identification + launch history pending pool decode',
  };
}

/**
 * H10 — circuit breakers clear. The full risk manager (Section 8) lands in
 * Phase 5; today this enforces the file-sentinel kill switch: presence of a
 * `KILL` file blocks all entries.
 */
export function checkBreakers(ctx: CheckContext): CheckResult {
  if (existsSync('KILL')) {
    return { id: 'H10', label: 'Circuit breakers clear', status: 'fail', detail: 'KILL sentinel present' };
  }
  return { id: 'H10', label: 'Circuit breakers clear', status: 'pass' };
}
