import type { Repositories } from '../../persistence/repositories.ts';
import type { ManipulationFeatures } from './types.ts';

/**
 * Copycat flag (P3.3): copycats graduate at 0.86 % vs 9.2 % for originals
 * [R4]. Two fingerprints, each checked against every earlier mint seen:
 *
 *  - name: lowercase alphanumerics of name + symbol;
 *  - image: the IPFS CID in the image link (content-addressed — the same
 *    picture re-uploaded yields the same CID), else the full URL.
 *
 * A byte-level perceptual hash would also catch re-encoded images; it needs an
 * image download + decoder on the hot path and is left out on purpose.
 */
export function nameFingerprint(name?: string, symbol?: string): string | null {
  const norm = (s?: string) => (s ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const n = norm(name);
  const sym = norm(symbol);
  return n || sym ? `${n}|${sym}` : null;
}

export function imageFingerprint(url?: string): string | null {
  if (!url) return null;
  const cid = /\/ipfs\/([A-Za-z0-9]{32,})/.exec(url)?.[1] ?? /^ipfs:\/\/([A-Za-z0-9]{32,})/.exec(url)?.[1];
  if (cid) return `cid:${cid}`;
  const m = /([A-Za-z0-9]{46,})(?:[/?#]|$)/.exec(url); // bare CID in a gateway subdomain / path
  return m ? `cid:${m[1]}` : `url:${url}`;
}

export function copycatFeatures(
  repos: Pick<Repositories, 'recordFingerprint'>,
  mint: string,
  meta: { name?: string; symbol?: string; links?: Record<string, string> } | undefined,
): NonNullable<ManipulationFeatures['copycat']> {
  const nameFp = nameFingerprint(meta?.name, meta?.symbol);
  const imageFp = imageFingerprint(meta?.links?.image);
  const nameMatches = nameFp ? repos.recordFingerprint('name', nameFp, mint) : 0;
  const imageMatches = imageFp ? repos.recordFingerprint('image', imageFp, mint) : 0;
  return { nameMatches, imageMatches, isCopycat: nameMatches > 0 || imageMatches > 0 };
}
