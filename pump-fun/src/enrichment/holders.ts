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
 * with -32602 "not a Token mint" for a slot or two while the RPC token index
 * catches up. Retry locally — do NOT mark the error retryable on RpcClient,
 * which would park the enrichment endpoint for 30s.
 */
const NOT_A_TOKEN_MINT_DELAYS_MS = [0, 180, 420];

export async function fetchHolders(
  rpc: RpcClient,
  mint: string,
  supplyHint?: SupplyHint | Promise<SupplyHint | undefined>,
  opts?: { largestRetryDelaysMs?: readonly number[] },
): Promise<HolderSnapshot> {
  // Start the largest-account read immediately. A DAS supply hint is optional
  // and must never delay this call — waiting on getAsset was marking H5/H6 unknown.
  // Attach a handler NOW: if the RPC rejects during resolveSupplyHint, Node would
  // otherwise emit unhandledRejection even though we await largestP later.
  const largestP = fetchLargestAccounts(rpc, mint, opts?.largestRetryDelaysMs ?? NOT_A_TOKEN_MINT_DELAYS_MS);
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
): Promise<Array<{ address: string; amount: bigint }>> {
  const waits = delaysMs.length > 0 ? delaysMs : [0];
  let lastErr: unknown;
  for (let i = 0; i < waits.length; i++) {
    const wait = waits[i] ?? 0;
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

/**
 * Use a DAS supply hint only if it is already in hand or resolves this tick.
 * Never wait on a slow/hung getAsset — fall through to getTokenSupply instead.
 */
async function resolveSupplyHint(
  supplyHint?: SupplyHint | Promise<SupplyHint | undefined>,
): Promise<SupplyHint | undefined> {
  if (supplyHint === undefined) return undefined;
  if (!isPromiseLike(supplyHint)) return supplyHint;
  return await Promise.race([
    supplyHint,
    new Promise<undefined>((resolve) => {
      setImmediate(() => resolve(undefined));
    }),
  ]);
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
