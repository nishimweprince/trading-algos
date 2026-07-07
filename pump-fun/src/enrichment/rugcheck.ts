import { logger } from '../core/logger.ts';

/**
 * RugCheck (api.rugcheck.xyz) advisory soft signal (Section 6.2). Never a
 * hard-fail input — "all hard-fail logic must be verifiable on-chain; APIs are
 * advisory only" (Section 13). Best-effort: any failure returns null and the
 * signal is simply omitted.
 *
 * IMPORTANT — direction: RugCheck's `score`/`score_normalised` are RISK scores
 * (higher = riskier; verified live: USDC=1, a thin pump token=9). Our soft-score
 * convention is "higher = safer", so we invert to goodness = 100 - normalised.
 *
 * Auth: the API key goes in the `apikey` header (verified: `Bearer`/`x-api-key`
 * return 401). The summary endpoint also works unauthenticated but is
 * rate-limited, so the key mainly raises limits for a soak.
 */

const BASE = 'https://api.rugcheck.xyz/v1';

export interface RugcheckResult {
  /** Goodness 0..100 (100 = safest) — already inverted from RugCheck's risk score. */
  score: number;
  /** RugCheck's raw normalised RISK score (higher = riskier), for the record. */
  riskNormalised: number;
  /** Named risks RugCheck flagged (e.g. "Low Liquidity"). */
  risks: string[];
}

export interface RugcheckOpts {
  apiKey?: string;
  timeoutMs?: number;
}

export async function fetchRugcheck(mint: string, opts: RugcheckOpts = {}): Promise<RugcheckResult | null> {
  const log = logger.child({ mod: 'rugcheck' });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? 1500);
  try {
    const headers: Record<string, string> = opts.apiKey ? { apikey: opts.apiKey } : {};
    const res = await fetch(`${BASE}/tokens/${mint}/report/summary`, { headers, signal: controller.signal });
    if (!res.ok) {
      log.debug('rugcheck non-ok', { mint, status: res.status });
      return null;
    }
    const body = (await res.json()) as { score_normalised?: number; risks?: Array<{ name?: string }> };
    if (typeof body.score_normalised !== 'number') return null;
    const riskNormalised = body.score_normalised;
    const score = Math.max(0, Math.min(100, 100 - riskNormalised)); // invert: risk -> goodness
    const risks = Array.isArray(body.risks) ? body.risks.map((r) => r.name ?? '').filter(Boolean) : [];
    return { score, riskNormalised, risks };
  } catch (err) {
    log.debug('rugcheck fetch failed', { mint, err });
    return null;
  } finally {
    clearTimeout(timer);
  }
}
