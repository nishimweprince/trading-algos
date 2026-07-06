import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';

/**
 * H4 — sellability (honeypot / transfer-tax trap). This requires building a real
 * sell swap and running it through `simulateTransaction`, which depends on the
 * swap builder that lands in Phase 4. Until then it returns `unknown` (→ veto in
 * live per the unknowns policy; logged in paper).
 *
 * H1/H2 (authorities) and H9 (Token-2022 extensions) already catch the static
 * honeypot vectors — freeze authority, transfer fee, transfer hook — on-chain.
 * H4 adds the dynamic simulation on top.
 */
export function checkSellability(_ctx: CheckContext): CheckResult {
  return {
    id: 'H4',
    label: 'Sellable (no honeypot)',
    status: 'unknown',
    detail: 'pending sell-simulation swap builder (Phase 4)',
  };
}
