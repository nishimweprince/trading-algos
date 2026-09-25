import type { RpcClient, SignatureInfo } from '../../core/rpc.ts';
import { parseSwaps, type SwapEvent } from '../txFlow.ts';
import type { ManipulationFeatures } from './types.ts';

type Rpc = Pick<RpcClient, 'getSignaturesForAddress' | 'getParsedTransaction'>;

/**
 * Bonding-curve history features (P3.3): creation-slot bundle share and wash
 * ratio, from the curve PDA's own transactions.
 *
 * The scan pages backwards from the newest signature for at most `maxPages`
 * pages of 1,000. When it reaches the curve's first transaction, the creation
 * slot is known and every buy in that slot is parsed: dev buy + same-slot
 * bundle as a share of supply. Coins whose curve history is longer than the
 * cap report `creationSlot: null` (unknown), never a partial guess.
 *
 * Wash ratio uses the newest `washSampleTx` swaps (the run-up to migration):
 * the share of that volume traded by wallets that both bought and sold.
 */
export async function curveFeatures(
  rpc: Rpc,
  curve: string,
  mint: string,
  supplyRaw: bigint,
  opts: { maxPages: number; maxCreationTx: number; washSampleTx: number; deadlineMs: number; now?: () => number },
): Promise<NonNullable<ManipulationFeatures['curve']>> {
  const now = opts.now ?? Date.now;
  const venue = new Set([curve]);
  const pages: SignatureInfo[][] = [];
  let before: string | undefined;
  let reachedStart = false;
  for (let p = 0; p < opts.maxPages && now() < opts.deadlineMs; p++) {
    const page = await rpc.getSignaturesForAddress(curve, { limit: 1000, ...(before ? { before } : {}) });
    pages.push(page);
    if (page.length < 1000) {
      reachedStart = true;
      break;
    }
    before = page[page.length - 1]!.signature;
  }
  const all = pages.flat();

  // Wash ratio over the newest swaps.
  const washSigs = all.filter((s) => !s.err).slice(0, opts.washSampleTx);
  const washSwaps = await parseMany(rpc, washSigs, mint, venue, opts.deadlineMs, now);
  const washRatio = washRatioOf(washSwaps);

  let creationSlot: number | null = null;
  let bundleSharePct: number | null = null;
  let creationSlotBuyers: number | null = null;
  if (reachedStart && all.length > 0) {
    creationSlot = Math.min(...all.map((s) => s.slot));
    const inSlot = all.filter((s) => s.slot === creationSlot && !s.err).slice(0, opts.maxCreationTx);
    const swaps = await parseMany(rpc, inSlot, mint, venue, opts.deadlineMs, now);
    const buys = swaps.filter((s) => s.side === 'buy');
    creationSlotBuyers = new Set(buys.map((s) => s.trader)).size;
    const bought = buys.reduce((a, s) => a + s.tokenAmount, 0n);
    bundleSharePct = supplyRaw > 0n ? (Number(bought) / Number(supplyRaw)) * 100 : null;
  }
  return { creationSlot, txScanned: all.length, bundleSharePct, creationSlotBuyers, washRatio };
}

/** Share of volume (token units) traded by wallets that appear on both sides. */
export function washRatioOf(swaps: readonly SwapEvent[]): number | null {
  if (swaps.length === 0) return null;
  const sides = new Map<string, Set<string>>();
  for (const s of swaps) {
    const set = sides.get(s.trader) ?? new Set<string>();
    set.add(s.side);
    sides.set(s.trader, set);
  }
  let total = 0;
  let both = 0;
  for (const s of swaps) {
    const v = Number(s.tokenAmount);
    total += v;
    if ((sides.get(s.trader)?.size ?? 0) > 1) both += v;
  }
  return total > 0 ? both / total : null;
}

async function parseMany(
  rpc: Rpc,
  sigs: readonly SignatureInfo[],
  mint: string,
  venue: ReadonlySet<string>,
  deadlineMs: number,
  now: () => number,
): Promise<SwapEvent[]> {
  const out: SwapEvent[] = [];
  for (let i = 0; i < sigs.length && now() < deadlineMs; i += 10) {
    const txs = await Promise.all(sigs.slice(i, i + 10).map((s) => rpc.getParsedTransaction(s.signature).catch(() => null)));
    for (const tx of txs) if (tx) out.push(...parseSwaps(tx, mint, venue));
  }
  return out;
}
