import type { RpcClient } from '../../core/rpc.ts';
import type { ManipulationFeatures } from './types.ts';

/**
 * Holder quality (P3.3): share of the top holders that are fresh wallets
 * (fewer than `freshTxThreshold` transactions ever) — the footprint of a
 * supply split across throwaway wallets to dodge the top-10 cap (H5).
 * One getSignaturesForAddress(limit = threshold) per sampled owner.
 */
export async function holderQuality(
  rpc: Pick<RpcClient, 'getSignaturesForAddress'>,
  owners: readonly string[],
  opts: { topN: number; freshTxThreshold: number },
): Promise<NonNullable<ManipulationFeatures['holderQuality']>> {
  const sample = [...new Set(owners)].slice(0, opts.topN);
  const counts = await Promise.all(
    sample.map((o) =>
      rpc
        .getSignaturesForAddress(o, { limit: opts.freshTxThreshold })
        .then((s) => s.length)
        .catch(() => null),
    ),
  );
  const known = counts.filter((c): c is number => c !== null);
  const fresh = known.filter((c) => c < opts.freshTxThreshold).length;
  return { sampled: known.length, freshWallets: fresh, freshRatio: known.length ? fresh / known.length : 0 };
}
