import type { CheckResult, CheckStatus } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';

function dasVerdict(id: string, label: string, authority: string | null | undefined): CheckResult {
  if (authority === undefined) return { id, label, status: 'unknown', detail: 'mint account unavailable (no DAS authority)' };
  const status: CheckStatus = authority === null ? 'pass' : 'fail';
  return {
    id,
    label,
    status,
    detail: authority === null ? 'revoked (via DAS)' : `${label === 'Mint authority revoked' ? 'mint' : 'freeze'} authority active: ${authority} (via DAS)`,
  };
}

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
    // DAS backstop: same on-chain data via the indexer, zero extra RPC (the
    // asset was already fetched for metadata). Only fields DAS explicitly
    // reports are used; anything absent stays unknown.
    const das = ctx.candidate.enrichment.dasAuthorities;
    if (das && (das.mintAuthority !== undefined || das.freezeAuthority !== undefined)) {
      return [dasVerdict('H1', 'Mint authority revoked', das.mintAuthority), dasVerdict('H2', 'Freeze authority revoked', das.freezeAuthority)];
    }
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
