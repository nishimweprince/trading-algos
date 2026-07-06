import type { RpcClient } from '../core/rpc.ts';
import type { Mint, Address } from '../core/types.ts';
import { LAMPORTS_PER_SOL } from '../core/constants.ts';
import { decodeTokenAccountAmount } from '../enrichment/pool.ts';
import { logger } from '../core/logger.ts';

/**
 * Local pool pricing (Section 7.2). Price is computed from the pool's vault
 * balances — never an external price API — so exit decisions run on data we
 * control. On the free tier we poll the vaults; the paid gRPC feed would give
 * per-slot updates as a drop-in later.
 *
 * Price is SOL per whole token: (quoteLamports / 1e9) / (baseReserve / 10^dec).
 */
export function computePrice(baseReserve: bigint, quoteReserveLamports: bigint, baseDecimals: number): number {
  if (baseReserve === 0n) return 0;
  const quoteSol = Number(quoteReserveLamports) / LAMPORTS_PER_SOL;
  const baseTokens = Number(baseReserve) / 10 ** baseDecimals;
  return baseTokens === 0 ? 0 : quoteSol / baseTokens;
}

export interface PoolRef {
  mint: Mint;
  baseVault: Address;
  quoteVault: Address;
  baseDecimals: number;
}

export interface PriceTick {
  mint: Mint;
  price: number;
  baseReserve: bigint;
  quoteReserveLamports: bigint;
  atMs: number;
}

/**
 * Polls the vault balances of every registered pool on a fixed cadence and
 * pushes a PriceTick per pool per tick (even when the price is unchanged, so
 * time-based exits still fire). All registered vaults are fetched in one
 * getMultipleAccounts call to minimise RPC load.
 */
export class PricePoller {
  private readonly rpc: RpcClient;
  private readonly intervalMs: number;
  private readonly log = logger.child({ mod: 'pricing' });
  private readonly refs = new Map<Mint, PoolRef>();
  private timer: NodeJS.Timeout | null = null;
  private polling = false;
  private onTick: (tick: PriceTick) => void = () => {};
  private readonly now: () => number;

  constructor(rpc: RpcClient, intervalMs: number, now: () => number = () => Date.now()) {
    this.rpc = rpc;
    this.intervalMs = intervalMs;
    this.now = now;
  }

  setHandler(handler: (tick: PriceTick) => void): void {
    this.onTick = handler;
  }

  register(ref: PoolRef): void {
    this.refs.set(ref.mint, ref);
  }

  unregister(mint: Mint): void {
    this.refs.delete(mint);
  }

  get size(): number {
    return this.refs.size;
  }

  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => void this.poll(), this.intervalMs);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.refs.clear();
  }

  private async poll(): Promise<void> {
    if (this.polling || this.refs.size === 0) return; // skip overlapping ticks
    this.polling = true;
    try {
      const refs = [...this.refs.values()];
      const addrs = refs.flatMap((r) => [r.baseVault, r.quoteVault]);
      const accts = await this.rpc.getMultipleAccountsBase64(addrs);
      const atMs = this.now();
      for (let i = 0; i < refs.length; i++) {
        const ref = refs[i]!;
        const baseAcct = accts[i * 2];
        const quoteAcct = accts[i * 2 + 1];
        if (!baseAcct || !quoteAcct) continue;
        const baseReserve = decodeTokenAccountAmount(baseAcct.data);
        const quoteReserveLamports = decodeTokenAccountAmount(quoteAcct.data);
        const price = computePrice(baseReserve, quoteReserveLamports, ref.baseDecimals);
        this.onTick({ mint: ref.mint, price, baseReserve, quoteReserveLamports, atMs });
      }
    } catch (err) {
      this.log.debug('price poll failed', { err });
    } finally {
      this.polling = false;
    }
  }
}
