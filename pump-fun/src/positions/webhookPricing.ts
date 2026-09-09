import { computePrice, type PoolRef, type PriceTick } from './pricing.ts';
import { logger } from '../core/logger.ts';

/**
 * Helius webhook price ingest (Section 7.2). A Helius webhook watching pool
 * vault addresses POSTs enhanced transactions to the dashboard; this module
 * turns each payload's token-balance changes into PriceTicks for the pools it
 * tracks — the same tick shape the PricePoller emits, so shadow/twin trackers
 * consume webhook and poll ticks through one handler.
 *
 * Polling stays running as the fallback: it guarantees liveness (ticks even
 * when prices are unchanged, so time-stops fire) while webhooks add
 * inter-tick freshness and save RPC quota. The LIVE position manager is
 * deliberately NOT wired here — exits need the poller's cadence guarantee,
 * not best-effort push delivery.
 *
 * Helius payload shape (enhanced, account-address webhooks): an array (or
 * single object) of transactions, each with
 * `accountData[].tokenBalanceChanges[]` entries of
 * `{ userAccount, mint, rawTokenAmount: { tokenAmount } }`.
 * Pool vaults are token accounts, so their balances arrive as userAccount
 * entries — no extra RPC per update.
 */

export type TickSink = (tick: PriceTick) => void;

/** token-account address → raw token amount (base units). */
export type VaultBalances = Map<string, bigint>;

export function parseHeliusWebhook(body: unknown): VaultBalances {
  const out: VaultBalances = new Map();
  const txs = Array.isArray(body) ? body : [body];
  for (const tx of txs) {
    const accountData = (tx as { accountData?: unknown })?.accountData;
    if (!Array.isArray(accountData)) continue;
    for (const acct of accountData) {
      const changes = (acct as { tokenBalanceChanges?: unknown })?.tokenBalanceChanges;
      if (!Array.isArray(changes)) continue;
      for (const ch of changes) {
        const c = ch as { userAccount?: unknown; rawTokenAmount?: { tokenAmount?: unknown } };
        const raw = c.rawTokenAmount?.tokenAmount;
        if (typeof c.userAccount !== 'string') continue;
        const amount = typeof raw === 'string' ? BigInt(raw) : typeof raw === 'number' ? BigInt(Math.trunc(raw)) : null;
        if (amount === null || amount < 0n) continue;
        out.set(c.userAccount, amount);
      }
    }
  }
  return out;
}

export class WebhookPriceIngest {
  private readonly refs = new Map<string, { ref: PoolRef; sink: TickSink }>();
  private readonly log = logger.child({ mod: 'webhook-pricing' });
  private readonly now: () => number;
  private ingestedTicks = 0;
  private ingestedPayloads = 0;

  constructor(now: () => number = () => Date.now()) {
    this.now = now;
  }

  register(ref: PoolRef, sink: TickSink): void {
    this.refs.set(ref.mint, { ref, sink });
  }

  unregister(mint: string): void {
    this.refs.delete(mint);
  }

  get size(): number {
    return this.refs.size;
  }

  get stats(): { tracked: number; payloads: number; ticks: number } {
    return { tracked: this.refs.size, payloads: this.ingestedPayloads, ticks: this.ingestedTicks };
  }

  /**
   * Turn vault balances from one webhook payload into ticks for every tracked
   * pool whose vaults are all present. Pools with a missing vault are skipped
   * (the poller still covers them). Returns the ticks emitted.
   */
  ingest(balances: VaultBalances, atMs?: number): PriceTick[] {
    const at = atMs ?? this.now();
    const emitted: PriceTick[] = [];
    if (balances.size === 0) return emitted;
    this.ingestedPayloads++;
    for (const { ref, sink } of this.refs.values()) {
      const base = balances.get(ref.baseVault);
      const quote = balances.get(ref.quoteVault);
      if (base === undefined || quote === undefined) continue;
      const tick: PriceTick = {
        mint: ref.mint,
        price: computePrice(base, quote, ref.baseDecimals),
        baseReserve: base,
        quoteReserveLamports: quote,
        atMs: at,
      };
      if (ref.creatorAta) {
        const creator = balances.get(ref.creatorAta);
        if (creator !== undefined) tick.creatorBaseBalance = creator;
      }
      this.ingestedTicks++;
      emitted.push(tick);
      try {
        sink(tick);
      } catch (err) {
        this.log.debug('webhook tick sink failed', { mint: ref.mint, err });
      }
    }
    return emitted;
  }
}
