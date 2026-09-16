import { logger } from '../core/logger.ts';

/**
 * pump.fun coin-age advisory soft signal. Never a hard-fail input — "all
 * hard-fail logic must be verifiable on-chain; APIs are advisory only"
 * (README Section 13, same rule `rugcheck.ts` follows). Helius DAS `getAsset`
 * carries no creation timestamp for fungible pump.fun mints (verified live),
 * and there is no O(1) on-chain way to get "time since mint creation" without
 * paginating the mint's full signature history — impractical inside the
 * enrichment budget. pump.fun's own (undocumented, third-party) frontend API
 * is the only fast source, so this is best-effort like RugCheck: any failure
 * returns null and the signal is simply omitted.
 *
 * Why this exists: a mint's bonding curve can only complete (graduate) once,
 * so a "graduation" for a mint created long ago is a red flag that something
 * — a detector bug, a replayed/misattributed event — is off, even though the
 * detector's own persistent per-mint guard (src/detector/index.ts) is the
 * real, on-chain-verifiable defense against reprocessing an already-seen
 * mint. This is a second, independent line of defense against the same
 * failure class using an orthogonal signal (creation time vs. "have we seen
 * this mint before").
 */

const BASE = 'https://frontend-api-v3.pump.fun/coins';

export interface TokenAgeResult {
  /** Mint creation time, ms since epoch, as reported by pump.fun. */
  createdAtMs: number;
}

export interface TokenAgeOpts {
  timeoutMs?: number;
}

export async function fetchTokenAge(mint: string, opts: TokenAgeOpts = {}): Promise<TokenAgeResult | null> {
  const log = logger.child({ mod: 'token-age' });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? 1500);
  try {
    const res = await fetch(`${BASE}/${mint}`, { signal: controller.signal });
    if (!res.ok) {
      log.debug('pump.fun coin lookup non-ok', { mint, status: res.status });
      return null;
    }
    const body = (await res.json()) as { created_timestamp?: number };
    if (typeof body.created_timestamp !== 'number' || !Number.isFinite(body.created_timestamp)) return null;
    return { createdAtMs: body.created_timestamp };
  } catch (err) {
    log.debug('pump.fun coin lookup failed', { mint, err });
    return null;
  } finally {
    clearTimeout(timer);
  }
}
