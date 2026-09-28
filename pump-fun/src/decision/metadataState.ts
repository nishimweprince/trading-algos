import type { TokenMetadata } from '../enrichment/types.ts';

/**
 * The metadata battery's state: the token's own text — name, symbol,
 * description and social links — and nothing numeric. Jev evaluates natural
 * language; flow / holder numbers belong to the entry battery and the learned
 * filter. Bump the version on ANY shape change (calibration is per version).
 *
 * The image link is dropped (Jev takes no images, and an IPFS hash is noise).
 * Copycat counts are not included: they come from the features phase, which
 * this call deliberately does not wait for.
 */
export const METADATA_STATE_VERSION = 1;

const LINK_KEYS = ['twitter', 'telegram', 'website', 'discord'] as const;

/** Null when there is nothing to judge (no name — the unindexed-mint case H11 handles). */
export function buildMetadataState(meta: TokenMetadata | undefined): Record<string, unknown> | null {
  if (!meta?.name) return null;
  const links: Record<string, string> = {};
  for (const k of LINK_KEYS) {
    const v = meta.links?.[k];
    if (typeof v === 'string' && v.length > 0) links[k] = v;
  }
  return {
    v: METADATA_STATE_VERSION,
    name: meta.name,
    symbol: meta.symbol ?? null,
    ...(meta.description ? { description: meta.description } : {}),
    links,
    hasSocials: meta.hasSocials,
  };
}
