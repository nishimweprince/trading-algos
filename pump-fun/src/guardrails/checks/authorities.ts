import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';

/**
 * H1 (mint authority) + H2 (freeze authority). Both must be revoked (null).
 * Verified directly from the decoded mint account — the authoritative on-chain
 * source, not an API (Section 6, "verify on-chain where possible").
 *
 * - Active mint authority → infinite supply inflation.
 * - Active freeze authority → freeze buyer accounts (classic honeypot).
 */
export function checkAuthorities(ctx: CheckContext): CheckResult[] {
  const mi = ctx.candidate.enrichment.mintInfo;
  if (!mi) {
    return [
      { id: 'H1', label: 'Mint authority revoked', status: 'unknown', detail: 'mint account unavailable' },
      { id: 'H2', label: 'Freeze authority revoked', status: 'unknown', detail: 'mint account unavailable' },
    ];
  }
  return [
    {
      id: 'H1',
      label: 'Mint authority revoked',
      status: mi.mintAuthority === null ? 'pass' : 'fail',
      ...(mi.mintAuthority ? { detail: `mint authority active: ${mi.mintAuthority}` } : {}),
    },
    {
      id: 'H2',
      label: 'Freeze authority revoked',
      status: mi.freezeAuthority === null ? 'pass' : 'fail',
      ...(mi.freezeAuthority ? { detail: `freeze authority active: ${mi.freezeAuthority}` } : {}),
    },
  ];
}
