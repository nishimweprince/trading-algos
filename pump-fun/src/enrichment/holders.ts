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
export async function fetchHolders(rpc: RpcClient, mint: string): Promise<HolderSnapshot> {
  const [supplyInfo, largest] = await Promise.all([
    rpc.getTokenSupply(mint),
    rpc.getTokenLargestAccounts(mint),
  ]);
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
