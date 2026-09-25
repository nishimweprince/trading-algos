import type { ParsedTx, RpcClient, SignatureInfo } from '../core/rpc.ts';
import { LAMPORTS_PER_SOL, WSOL_MINT } from '../core/constants.ts';

/**
 * Trade-level flow from on-chain transactions (work plan 2026-09-25 P3.1 /
 * P3.3). Shared by early-flow tx stats and the manipulation features
 * (bundle share, wash ratio, sniper concentration).
 *
 * A swap is recovered from token-balance deltas, which works identically on
 * the pump.fun bonding curve and on PumpSwap: every owner whose balance of the
 * traded mint rose bought, every owner whose balance fell sold — except the
 * venue's own accounts (curve / pool), passed as `venueOwners`. SOL size is
 * the fee payer's native lamport delta (less the fee) when the WSOL leg is
 * wrapped in-tx, or the venue's WSOL vault delta when one is given.
 */

export interface SwapEvent {
  signature: string;
  slot: number;
  blockTime: number | null;
  trader: string;
  /** First signer — the wallet that paid for the tx (bundlers fund many traders from one payer). */
  feePayer: string;
  side: 'buy' | 'sell';
  /** Base-token amount moved, raw units. */
  tokenAmount: bigint;
  /** SOL moved by this trader, best effort (0 when not recoverable). */
  sol: number;
}

export function parseSwaps(
  tx: ParsedTx,
  mint: string,
  venueOwners: ReadonlySet<string>,
  opts: { quoteVault?: string } = {},
): SwapEvent[] {
  if (!tx.meta || tx.meta.err) return [];
  const keys = tx.transaction.message.accountKeys;
  const feePayer = keys.find((k) => k.signer)?.pubkey ?? keys[0]?.pubkey ?? '';
  const signature = tx.transaction.signatures[0] ?? '';

  const delta = new Map<string, bigint>();
  const add = (owner: string | undefined, amt: bigint) => {
    if (!owner || venueOwners.has(owner)) return;
    delta.set(owner, (delta.get(owner) ?? 0n) + amt);
  };
  for (const b of tx.meta.postTokenBalances ?? []) if (b.mint === mint) add(b.owner, BigInt(b.uiTokenAmount.amount));
  for (const b of tx.meta.preTokenBalances ?? []) if (b.mint === mint) add(b.owner, -BigInt(b.uiTokenAmount.amount));

  // Venue SOL leg: the WSOL vault's token delta (AMM) — split across traders
  // pro rata by token amount when several trade in one tx.
  let venueSol = 0;
  if (opts.quoteVault) {
    const idx = keys.findIndex((k) => k.pubkey === opts.quoteVault);
    if (idx >= 0) {
      const pre = tx.meta.preTokenBalances?.find((b) => b.accountIndex === idx && b.mint === WSOL_MINT);
      const post = tx.meta.postTokenBalances?.find((b) => b.accountIndex === idx && b.mint === WSOL_MINT);
      if (pre && post) venueSol = Math.abs(Number(BigInt(post.uiTokenAmount.amount) - BigInt(pre.uiTokenAmount.amount))) / LAMPORTS_PER_SOL;
    }
  }
  const moved = [...delta.values()].reduce((s, d) => s + (d < 0n ? -d : d), 0n);

  const out: SwapEvent[] = [];
  for (const [trader, d] of delta) {
    if (d === 0n) continue;
    const amt = d < 0n ? -d : d;
    let sol = 0;
    if (venueSol > 0 && moved > 0n) sol = venueSol * (Number(amt) / Number(moved));
    else {
      const idx = keys.findIndex((k) => k.pubkey === trader);
      if (idx >= 0) {
        const lam = (tx.meta.postBalances[idx] ?? 0) - (tx.meta.preBalances[idx] ?? 0);
        sol = Math.abs(lam) / LAMPORTS_PER_SOL;
      }
    }
    out.push({ signature, slot: tx.slot, blockTime: tx.blockTime, trader, feePayer, side: d > 0n ? 'buy' : 'sell', tokenAmount: amt, sol });
  }
  return out;
}

/**
 * Pull up to `maxTx` recent transactions touching `address` and parse the
 * swaps in `mint`. Bounded (limit + deadline) so it can sit next to the
 * screening path; returns whatever parsed before the deadline.
 */
export async function fetchSwaps(
  rpc: Pick<RpcClient, 'getSignaturesForAddress' | 'getParsedTransaction'>,
  address: string,
  mint: string,
  venueOwners: ReadonlySet<string>,
  opts: { maxTx: number; deadlineMs?: number; quoteVault?: string; before?: string; oldestFirst?: boolean; now?: () => number } ,
): Promise<{ swaps: SwapEvent[]; signatures: SignatureInfo[]; complete: boolean }> {
  const now = opts.now ?? Date.now;
  const sigs = (await rpc.getSignaturesForAddress(address, { limit: Math.min(1000, Math.max(1, opts.maxTx)), ...(opts.before ? { before: opts.before } : {}) }))
    .filter((s) => !s.err);
  const ordered = opts.oldestFirst ? [...sigs].reverse() : sigs;
  const swaps: SwapEvent[] = [];
  let complete = true;
  const batch = 10;
  for (let i = 0; i < ordered.length; i += batch) {
    if (opts.deadlineMs !== undefined && now() >= opts.deadlineMs) {
      complete = false;
      break;
    }
    const txs = await Promise.all(
      ordered.slice(i, i + batch).map((s) => rpc.getParsedTransaction(s.signature).catch(() => null)),
    );
    for (const tx of txs) {
      if (!tx) {
        complete = false;
        continue;
      }
      swaps.push(...parseSwaps(tx, mint, venueOwners, opts.quoteVault ? { quoteVault: opts.quoteVault } : {}));
    }
  }
  return { swaps, signatures: sigs, complete };
}

export interface FlowStats {
  buyCount: number;
  sellCount: number;
  uniqueBuyers: number;
  uniqueSellers: number;
  buySol: number;
  sellSol: number;
  /** Largest single sell, SOL. */
  maxSellSol: number;
}

export function flowStats(swaps: readonly SwapEvent[]): FlowStats {
  const buys = swaps.filter((s) => s.side === 'buy');
  const sells = swaps.filter((s) => s.side === 'sell');
  return {
    buyCount: buys.length,
    sellCount: sells.length,
    uniqueBuyers: new Set(buys.map((s) => s.trader)).size,
    uniqueSellers: new Set(sells.map((s) => s.trader)).size,
    buySol: buys.reduce((a, s) => a + s.sol, 0),
    sellSol: sells.reduce((a, s) => a + s.sol, 0),
    maxSellSol: sells.reduce((m, s) => Math.max(m, s.sol), 0),
  };
}
