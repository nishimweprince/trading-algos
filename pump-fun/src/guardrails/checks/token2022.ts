import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';
import { RUG_EXTENSION_IDS, RUG_EXTENSION_NAMES } from '../../enrichment/mint.ts';

/**
 * H9 — Token-2022 extension screen. A plain SPL mint passes trivially. A
 * Token-2022 mint is rejected if it carries any programmable-rug extension
 * (Section 6): transfer fee, transfer hook, permanent delegate,
 * default-account-state, or non-transferable (honeypot). Benign extensions
 * (metadata pointer/group) pass.
 */
export function checkToken2022(ctx: CheckContext): CheckResult {
  const mi = ctx.candidate.enrichment.mintInfo;
  if (!mi) {
    return { id: 'H9', label: 'Token-2022 extensions safe', status: 'unknown', detail: 'mint account unavailable' };
  }
  if (!mi.isToken2022) {
    return { id: 'H9', label: 'Token-2022 extensions safe', status: 'pass', detail: 'legacy SPL mint' };
  }
  const bad = mi.extensions.filter((e) => RUG_EXTENSION_IDS.has(e));
  if (bad.length > 0) {
    return {
      id: 'H9',
      label: 'Token-2022 extensions safe',
      status: 'fail',
      detail: `rug extensions: ${bad.map((e) => RUG_EXTENSION_NAMES[e] ?? e).join(', ')}`,
    };
  }
  return { id: 'H9', label: 'Token-2022 extensions safe', status: 'pass', detail: 'no rug extensions' };
}
