import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';
import { MintExtension } from '../../enrichment/mint.ts';

/**
 * H9 — Token-2022 extension screen. A plain SPL mint passes trivially. A
 * Token-2022 mint is rejected if it carries any programmable-rug extension
 * (Section 6): transfer fee, transfer hook, permanent delegate,
 * default-account-state, or non-transferable (honeypot).
 */
const RUG_EXTENSIONS = new Set<number>([
  MintExtension.TransferFeeConfig,
  MintExtension.TransferHook,
  MintExtension.PermanentDelegate,
  MintExtension.DefaultAccountState,
  MintExtension.NonTransferable,
]);

const EXT_NAMES: Record<number, string> = {
  [MintExtension.TransferFeeConfig]: 'TransferFee',
  [MintExtension.TransferHook]: 'TransferHook',
  [MintExtension.PermanentDelegate]: 'PermanentDelegate',
  [MintExtension.DefaultAccountState]: 'DefaultAccountState',
  [MintExtension.NonTransferable]: 'NonTransferable',
};

export function checkToken2022(ctx: CheckContext): CheckResult {
  const mi = ctx.candidate.enrichment.mintInfo;
  if (!mi) {
    return { id: 'H9', label: 'Token-2022 extensions safe', status: 'unknown', detail: 'mint account unavailable' };
  }
  if (!mi.isToken2022) {
    return { id: 'H9', label: 'Token-2022 extensions safe', status: 'pass', detail: 'legacy SPL mint' };
  }
  const bad = mi.extensions.filter((e) => RUG_EXTENSIONS.has(e));
  if (bad.length > 0) {
    return {
      id: 'H9',
      label: 'Token-2022 extensions safe',
      status: 'fail',
      detail: `rug extensions: ${bad.map((e) => EXT_NAMES[e] ?? e).join(', ')}`,
    };
  }
  return { id: 'H9', label: 'Token-2022 extensions safe', status: 'pass', detail: 'no rug extensions' };
}
