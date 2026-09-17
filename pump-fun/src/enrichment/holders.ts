import type { RpcClient } from '../core/rpc.ts';
import { base58Encode } from '../core/base58.ts';
import type { HolderInfo, HolderSnapshot } from './types.ts';

/**
 * Holder concentration snapshot (Section 5 / H5 input). Fetches the largest
 * token accounts and resolves the wallet behind each (owner at offset 32 of an
 * SPL token account) so downstream checks can exclude the pool vault and burn
 * addresses.
 *
 * NOTE: pool-vault identification requires the decoded PumpSwap pool (pending —
 * see guardrails/checks/pending.ts). Until then `top10Share`/`maxShare` are RAW
 * (pool included); the H5 check treats concentration as unknown rather than
 * flagging the pool as a whale.
 */
export interface SupplyHint {
  supply: bigint;
  decimals: number;
}

/**
 * Brand-new mints (and some Token-2022 accounts) fail getTokenLargestAccounts
 * with -32602 "not a Token mint" for a few seconds while the RPC token index
 * catches up. Retry locally — do NOT mark the error retryable on RpcClient,
 * which would park the enrichment endpoint for 30s. Production passes
 * config.guardrails.holdersNotMintRetryDelaysMs; this is the bare default.
 */
const NOT_A_TOKEN_MINT_DELAYS_MS = [0, 180, 420];
/** Headroom a retry must leave before the enrichment deadline to be worth starting. */
const RETRY_DEADLINE_MARGIN_MS = 150;

export interface FetchHoldersOptions {
  /** ms between "not a Token mint" retries (first entry = initial wait, usually 0). */
  largestRetryDelaysMs?: readonly number[];
  /** Enrichment deadline (epoch ms): a retry that cannot finish before it is skipped. */
  deadlineMs?: number;
  now?: () => number;
}

/** One holder-supply source: a value, a promise for one, or absent. */
export type SupplyHintInput = SupplyHint | Promise<SupplyHint | undefined> | undefined;

export async function fetchHolders(
  rpc: RpcClient,
  mint: string,
  supplyHint?: SupplyHintInput | readonly SupplyHintInput[],
  opts?: FetchHoldersOptions,
): Promise<HolderSnapshot> {
  // Start the largest-account read immediately. A DAS supply hint is optional
  // and must never delay this call — waiting on getAsset was marking H5/H6 unknown.
  // Attach a handler NOW: if the RPC rejects during resolveSupplyHint, Node would
  // otherwise emit unhandledRejection even though we await largestP later.
  const largestP = fetchLargestAccounts(rpc, mint, opts?.largestRetryDelaysMs ?? NOT_A_TOKEN_MINT_DELAYS_MS, opts);
  void largestP.catch(() => {});
  const hint = await resolveSupplyHint(supplyHint);
  const supplyInfo = hint
    ? { amount: hint.supply, decimals: hint.decimals }
    : await rpc.getTokenSupply(mint);
  const largest = await largestP;
  const supply = supplyInfo.amount;

  // Resolve owners of the top accounts in one batch.
  const owners = await rpc.getMultipleAccountsBase64(largest.map((a) => a.address));

  const holders: HolderInfo[] = largest.map((a, i) => {
    const acct = owners[i];
    const share = supply > 0n ? fraction(a.amount, supply) : 0;
    const holder: HolderInfo = { account: a.address, amount: a.amount, share };
    const owner = acct ? decodeTokenAccountOwner(acct.data) : undefined;
    if (owner) holder.owner = owner;
    return holder;
  });

  const sorted = [...holders].sort((a, b) => (b.amount > a.amount ? 1 : b.amount < a.amount ? -1 : 0));
  const top10Share = sorted.slice(0, 10).reduce((s, h) => s + h.share, 0);
  const maxShare = sorted.length > 0 ? (sorted[0]?.share ?? 0) : 0;

  return { supply, decimals: supplyInfo.decimals, holders: sorted, top10Share, maxShare };
}

function isPromiseLike<T>(v: T | Promise<T> | undefined): v is Promise<T> {
  return typeof v === 'object' && v !== null && 'then' in v;
}

export function isNotATokenMintError(err: unknown): boolean {
  return err instanceof Error && /not a Token mint/i.test(err.message);
}

async function fetchLargestAccounts(
  rpc: RpcClient,
  mint: string,
  delaysMs: readonly number[],
  opts?: Pick<FetchHoldersOptions, 'deadlineMs' | 'now'>,
): Promise<Array<{ address: string; amount: bigint }>> {
  const waits = delaysMs.length > 0 ? delaysMs : [0];
  const now = opts?.now ?? Date.now;
  let lastErr: unknown;
  for (let i = 0; i < waits.length; i++) {
    const wait = waits[i] ?? 0;
    // A retry that cannot complete inside the enrichment budget would only
    // convert into a budget timeout — surface the last error now instead.
    if (i > 0 && opts?.deadlineMs !== undefined && now() + wait > opts.deadlineMs - RETRY_DEADLINE_MARGIN_MS) {
      throw lastErr instanceof Error ? lastErr : new Error('getTokenLargestAccounts failed');
    }
    if (wait > 0) await delay(wait);
    try {
      return await rpc.getTokenLargestAccounts(mint);
    } catch (err) {
      lastErr = err;
      if (!isNotATokenMintError(err) || i === waits.length - 1) throw err;
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error('getTokenLargestAccounts failed');
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const TICK = Symbol('hintTick');
const nextTick = (): Promise<typeof TICK> =>
  new Promise((resolve) => {
    setImmediate(() => resolve(TICK));
  });

/**
 * Use a supply hint only if one is already in hand or resolves promptly.
 * First DEFINED hint wins across sources (mint account, then DAS); a tick
 * with nothing ready falls through to getTokenSupply. Never waits on a
 * slow/hung hint — same latency guarantee as before, now across sources.
 */
async function resolveSupplyHint(
  supplyHint?: SupplyHintInput | readonly SupplyHintInput[],
): Promise<SupplyHint | undefined> {
  const list = supplyHint === undefined ? [] : Array.isArray(supplyHint) ? [...supplyHint] : [supplyHint];
  for (const h of list) {
    if (h !== undefined && !isPromiseLike(h)) return h;
  }
  const pending = new Map(
    list
      .filter((h): h is Promise<SupplyHint | undefined> => isPromiseLike(h))
      .map((p) => [p, p.then((value) => ({ owner: p, value })) ] as const),
  );
  while (pending.size > 0) {
    const settled = await Promise.race([...pending.values(), nextTick()]);
    if (settled === TICK) return undefined;
    pending.delete(settled.owner);
    if (settled.value !== undefined) return settled.value;
  }
  return undefined;
}

/** SPL token account: owner pubkey is at byte offset 32 (after the 32-byte mint). */
function decodeTokenAccountOwner(base64Data: string): string | undefined {
  const buf = Buffer.from(base64Data, 'base64');
  if (buf.length < 64) return undefined;
  return base58Encode(buf.subarray(32, 64));
}

/** Ratio of two bigints as a JS number in [0,1], via a fixed-point scale. */
function fraction(part: bigint, whole: bigint): number {
  const SCALE = 1_000_000n;
  if (whole === 0n) return 0;
  return Number((part * SCALE) / whole) / Number(SCALE);
}
