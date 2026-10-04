import { WSOL_MINT } from '../core/constants.ts';
import type { ParsedTx, RpcClient } from '../core/rpc.ts';
import type { ExitTrigger } from '../core/types.ts';
import { decodeVirtualQuote } from '../enrichment/pool.ts';
import type { ExitCfg } from '../exits/engine.ts';
import { PaperPosition } from '../positions/position.ts';
import { computePrice } from '../positions/pricing.ts';

/**
 * Pool replay (feat/fra-lean WS6): re-run the configured exits against a
 * pool's real on-chain swap history, priced the way the program prices
 * (vault + virtual quote reserve).
 *
 * The 2026-10 live trades cannot answer "is there an edge?": their marks
 * ignored the virtual reserve, so every exit fired on a phantom −21.7 %.
 * This rebuilds each traded pool's first minutes from chain, enters where we
 * actually entered (our buy's position in the pool's history) — or at the
 * first swap, for the latency counterfactual — and walks PaperPosition, the
 * same exit FSM live uses, over every subsequent swap.
 */

/** One pool state after a transaction that touched it. */
export interface PricePoint {
  signature: string;
  slot: number;
  /** Order within the fetched history (ascending). */
  index: number;
  signer: string | null;
  quoteVault: bigint;
  baseVault: bigint;
  price: number;
  err: boolean;
}

export interface ReplayCfg {
  exits: ExitCfg;
  /** Swap fee per side, bps (PumpSwap graduation tier: 125). */
  feeBps: number;
  /** Network + priority + tip per transaction, SOL. */
  txCostSol: number;
  /** Force-close after this long (ms of slot time) if no exit fired. */
  horizonMs: number;
}

export interface ReplayOutcome {
  entryIndex: number;
  entrySlotOffset: number;
  entryPrice: number;
  exitReason: ExitTrigger | 'HORIZON' | 'NO_DATA';
  holdMs: number;
  mfePct: number;
  maePct: number;
  /** Net of swap fees and tx costs, % of size. */
  netPct: number;
  netSol: number;
}

const SLOT_MS = 400;

/** Price path from parsed transactions (ascending), effective quote = vault + virtual. */
export function buildPricePath(txs: ParsedTx[], pool: string, mint: string, virtualQuoteLamports: bigint, baseDecimals: number): PricePoint[] {
  const out: PricePoint[] = [];
  txs.forEach((tx, index) => {
    const post = tx.meta?.postTokenBalances ?? [];
    const own = post.filter((b) => b.owner === pool);
    const q = own.find((b) => b.mint === WSOL_MINT)?.uiTokenAmount.amount;
    const b = own.find((x) => x.mint === mint)?.uiTokenAmount.amount;
    if (q === undefined || b === undefined) return;
    const quoteVault = BigInt(q);
    const baseVault = BigInt(b);
    out.push({
      signature: tx.transaction.signatures[0] ?? '',
      slot: tx.slot,
      index,
      signer: tx.transaction.message.accountKeys.find((k) => k.signer)?.pubkey ?? null,
      quoteVault,
      baseVault,
      price: computePrice(baseVault, quoteVault, baseDecimals, virtualQuoteLamports),
      err: Boolean(tx.meta?.err),
    });
  });
  return out;
}

/**
 * Enter after point `entryIdx` (its post-state is the price we fill at, plus
 * fee and impact), then walk every later successful point through the exit
 * FSM. Time is slot time relative to the entry.
 */
export function simulateTrade(path: PricePoint[], entryIdx: number, sizeSol: number, cfg: ReplayCfg, virtualQuoteLamports: bigint): ReplayOutcome {
  const at = path[entryIdx];
  if (!at || !(at.price > 0)) {
    return { entryIndex: entryIdx, entrySlotOffset: 0, entryPrice: 0, exitReason: 'NO_DATA', holdMs: 0, mfePct: 0, maePct: 0, netPct: 0, netSol: 0 };
  }
  const fee = cfg.feeBps / 10_000;
  const effQuoteSol = Number(at.quoteVault + virtualQuoteLamports) / 1e9;
  const impact = sizeSol / (effQuoteSol + sizeSol);
  const entryPrice = (at.price * (1 + fee)) / (1 - impact);
  const pos = new PaperPosition({ mint: '', sizeSol, entryPrice, openedAtMs: 0, highVolatility: false, cfg: cfg.exits });
  let proceeds = 0;
  let mfe = 0;
  let mae = 0;
  let lastMs = 0;
  let lastPrice = at.price;
  let lastQuote = at.quoteVault;
  const sell = (fraction: number, price: number) => {
    const value = fraction * sizeSol * (price / entryPrice);
    const effOut = Number(lastQuote + virtualQuoteLamports) / 1e9;
    const sellImpact = value / (effOut + value);
    proceeds += value * (1 - fee) * (1 - sellImpact);
  };
  for (let i = entryIdx + 1; i < path.length && pos.state === 'OPEN'; i++) {
    const p = path[i]!;
    if (p.err || !(p.price > 0)) continue;
    const t = (p.slot - at.slot) * SLOT_MS + (i - entryIdx); // +1 ms per tx keeps same-slot order
    if (t > cfg.horizonMs) break;
    lastMs = t;
    lastPrice = p.price;
    lastQuote = p.quoteVault;
    const g = (p.price / entryPrice - 1) * 100;
    mfe = Math.max(mfe, g);
    mae = Math.min(mae, g);
    for (const f of pos.onPrice(p.price, t)) sell(f.fraction, f.price);
  }
  let exitReason: ReplayOutcome['exitReason'] = pos.lastTrigger ?? 'HORIZON';
  if (pos.state === 'OPEN') {
    const f = pos.forceClose(lastPrice, lastMs, 'TIME_STOP');
    if (f) sell(f.fraction, f.price);
    exitReason = 'HORIZON';
  }
  const txs = 2;
  const netSol = proceeds - sizeSol - txs * cfg.txCostSol;
  return {
    entryIndex: entryIdx,
    entrySlotOffset: at.slot - (path[0]?.slot ?? at.slot),
    entryPrice,
    exitReason,
    holdMs: lastMs,
    mfePct: mfe,
    maePct: mae,
    netPct: (netSol / sizeSol) * 100,
    netSol,
  };
}

/** Oldest-first transaction history of a pool, capped at `maxTx`. */
export async function fetchPoolHistory(
  rpc: Pick<RpcClient, 'getSignaturesForAddress' | 'getParsedTransaction'>,
  pool: string,
  opts: { maxTx: number; maxPages?: number; concurrency?: number },
): Promise<ParsedTx[]> {
  const sigs: string[] = [];
  let before: string | undefined;
  for (let page = 0; page < (opts.maxPages ?? 20); page++) {
    const batch = await rpc.getSignaturesForAddress(pool, { limit: 1000, ...(before ? { before } : {}) });
    sigs.push(...batch.map((s) => s.signature));
    if (batch.length < 1000) break;
    before = batch[batch.length - 1]!.signature;
  }
  const oldest = sigs.reverse().slice(0, opts.maxTx);
  const out: Array<ParsedTx | null> = new Array(oldest.length).fill(null);
  let next = 0;
  const worker = async () => {
    while (next < oldest.length) {
      const i = next++;
      out[i] = await rpc.getParsedTransaction(oldest[i]!).catch(() => null);
    }
  };
  await Promise.all(Array.from({ length: opts.concurrency ?? 8 }, worker));
  return out.filter((t): t is ParsedTx => t !== null).sort((a, b) => a.slot - b.slot);
}

/** The pool account's virtual quote reserve (constant once the pool exists). */
export async function readPoolVirtualQuote(rpc: Pick<RpcClient, 'getAccountInfoBase64'>, pool: string): Promise<bigint> {
  const acct = await rpc.getAccountInfoBase64(pool, 'confirmed');
  return acct ? decodeVirtualQuote(Buffer.from(acct.data, 'base64')) : 0n;
}
