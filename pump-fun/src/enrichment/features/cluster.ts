import type { ParsedTx, RpcClient } from '../../core/rpc.ts';
import type { Repositories } from '../../persistence/repositories.ts';

type Rpc = Pick<RpcClient, 'getSignaturesForAddress' | 'getParsedTransaction'>;

/**
 * Creator funding cluster (P3.3). The first SOL that ever reached a wallet
 * identifies who set it up; creators sharing a funder (1 hop) or a funder's
 * funder (2 hops) are one operator. Launch counts are then taken over the
 * whole cluster, extending `creatorMaxLaunches7d` from wallets to operators.
 *
 * High-traffic funders (exchanges, routers — history longer than `maxSigs`)
 * are "hubs": they fund thousands of unrelated wallets, so clustering through
 * them would be wrong. A hub funder leaves the creator as its own root.
 */
export interface FunderResolution {
  funder: string | null;
  /** True when the wallet's history is longer than the scan cap (first tx unreachable). */
  truncated: boolean;
}

/** Account that paid the most SOL into `wallet` in `tx` (fee payer preferred). */
export function funderFromTx(tx: ParsedTx, wallet: string): string | null {
  if (!tx.meta) return null;
  const keys = tx.transaction.message.accountKeys;
  const walletIdx = keys.findIndex((k) => k.pubkey === wallet);
  if (walletIdx < 0) return null;
  const gained = (tx.meta.postBalances[walletIdx] ?? 0) - (tx.meta.preBalances[walletIdx] ?? 0);
  if (gained <= 0) return null;
  const payer = keys.find((k) => k.signer)?.pubkey;
  if (payer && payer !== wallet) return payer;
  let best: string | null = null;
  let bestDrop = 0;
  keys.forEach((k, i) => {
    if (i === walletIdx) return;
    const drop = (tx.meta!.preBalances[i] ?? 0) - (tx.meta!.postBalances[i] ?? 0);
    if (drop > bestDrop) {
      bestDrop = drop;
      best = k.pubkey;
    }
  });
  return best;
}

export async function resolveFunder(rpc: Rpc, wallet: string, maxSigs: number): Promise<FunderResolution> {
  const sigs = await rpc.getSignaturesForAddress(wallet, { limit: maxSigs });
  if (sigs.length === 0) return { funder: null, truncated: false };
  if (sigs.length >= maxSigs) return { funder: null, truncated: true };
  // Oldest first: the first transfer in is usually the very first tx.
  for (const s of [...sigs].reverse().slice(0, 3)) {
    const tx = await rpc.getParsedTransaction(s.signature);
    const funder = tx ? funderFromTx(tx, wallet) : null;
    if (funder) return { funder, truncated: false };
  }
  return { funder: null, truncated: false };
}

export async function creatorCluster(
  rpc: Rpc,
  repos: Pick<Repositories, 'walletFunder' | 'upsertWalletFunder' | 'clusterLaunchCount'>,
  creator: string,
  opts: { hops: 1 | 2; maxSigs: number; days?: number },
): Promise<NonNullable<import('./types.ts').ManipulationFeatures['cluster']>> {
  let cached = repos.walletFunder(creator);
  let hubFunder = false;
  if (!cached) {
    const hop1 = await resolveFunder(rpc, creator, opts.maxSigs);
    let root = creator;
    if (hop1.funder) {
      root = hop1.funder;
      if (opts.hops === 2) {
        const known = repos.walletFunder(hop1.funder);
        if (known) root = known.root ?? hop1.funder;
        else {
          const hop2 = await resolveFunder(rpc, hop1.funder, opts.maxSigs);
          if (hop2.truncated) {
            // The funder itself is a hub: do not cluster through it.
            hubFunder = true;
            root = creator;
          } else {
            root = hop2.funder ?? hop1.funder;
          }
          repos.upsertWalletFunder(hop1.funder, hop2.funder, hubFunder ? hop1.funder : root);
        }
      }
    }
    repos.upsertWalletFunder(creator, hop1.funder, root);
    cached = { funder: hop1.funder, root };
  }
  const root = cached.root ?? creator;
  const counts = repos.clusterLaunchCount(root, opts.days ?? 7);
  return { funder: cached.funder, root, launches7d: counts.launches, wallets: counts.wallets, hubFunder };
}
