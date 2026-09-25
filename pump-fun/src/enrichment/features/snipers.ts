import type { Repositories } from '../../persistence/repositories.ts';
import type { SwapEvent } from '../txFlow.ts';
import type { ManipulationFeatures } from './types.ts';

/**
 * Sniper concentration post-migration (P3.3). A wallet that is an early
 * buyer (first `windowSec` after migration) across `minCoins` other
 * graduations is a known sniper; a pool whose early buying is dominated by
 * them is expected to be dumped into (F11: holds < 1 s ran WR 32 %).
 *
 * Built entirely from the early-flow swaps the momentum sampler already
 * parsed — no extra RPC. Every observation is recorded, so the sniper list
 * grows from the bot's own logs.
 */
export function sniperFeatures(
  repos: Pick<Repositories, 'recordSniperObservations' | 'sniperCounts'>,
  mint: string,
  swaps: readonly SwapEvent[],
  opts: { windowSec: number; minCoins: number; migrationBlockTime?: number | null },
): NonNullable<ManipulationFeatures['snipers']> {
  const buys = swaps.filter((s) => s.side === 'buy');
  const t0 = opts.migrationBlockTime ?? Math.min(...buys.map((s) => s.blockTime ?? Infinity));
  const early = Number.isFinite(t0)
    ? buys.filter((s) => s.blockTime !== null && s.blockTime - t0 <= opts.windowSec)
    : buys;
  const wallets = [...new Set(early.map((s) => s.trader))];
  const counts = repos.sniperCounts(wallets, mint);
  repos.recordSniperObservations(mint, wallets);
  const known = new Set(wallets.filter((w) => (counts.get(w) ?? 0) >= opts.minCoins));
  const vol = early.reduce((a, s) => a + s.sol, 0);
  const sniperVol = early.filter((s) => known.has(s.trader)).reduce((a, s) => a + s.sol, 0);
  return { earlyBuyers: wallets.length, knownSnipers: known.size, sniperBuyShare: vol > 0 ? sniperVol / vol : 0 };
}
