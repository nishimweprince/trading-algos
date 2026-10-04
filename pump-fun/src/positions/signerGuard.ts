import type { TypedBus } from '../core/bus.ts';
import type { RpcClient } from '../core/rpc.ts';
import { logger } from '../core/logger.ts';
import { isOwnSignature, readAllowListedSignatures } from '../core/ownSignatures.ts';
import type { WalletTx, WalletWatch } from './walletStream.ts';

/**
 * Foreign-signer guard.
 *
 * A transaction paid by our wallet that no code in this process signed is a
 * second writer: another bot instance with the same key (a forgotten box in
 * Kigali was found running in 2026-10), a leaked key, or a human acting by
 * hand. Two writers race each other's positions, double the RPC load on a
 * shared key, and make every P&L number wrong. On the first one:
 *  - new entries halt (open positions keep their exits);
 *  - a critical Telegram alert names the signature.
 * Cleared only by a restart, after the operator has found the other writer.
 *
 * Operator CLIs allow-list their own signatures (core/ownSignatures.ts).
 */
export interface SignerGuardDeps {
  wallet: string;
  watch: WalletWatch;
  bus: TypedBus;
  /** Stop new entries (PositionManager.haltEntries). */
  halt: (reason: string) => void;
  allowListPath: string;
  graceMs: number;
  sleep?: (ms: number) => Promise<void>;
}

export class SignerGuard {
  private readonly deps: SignerGuardDeps;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly log = logger.child({ mod: 'signer-guard' });
  private unsubscribe: (() => void) | null = null;
  private tripped = false;
  readonly foreign: string[] = [];

  constructor(deps: SignerGuardDeps) {
    this.deps = deps;
    this.sleep = deps.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  }

  start(): void {
    this.unsubscribe = this.deps.watch.onTransaction((tx) => void this.check(tx));
    this.log.info('foreign-signer guard armed', { wallet: short(this.deps.wallet) });
  }

  stop(): void {
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  /** Judge one wallet tx (exposed for tests). */
  async check(tx: WalletTx): Promise<void> {
    if (tx.feePayer !== this.deps.wallet) return; // someone else paid: a transfer to us, not our key
    if (isOwnSignature(tx.signature)) return;
    if (this.deps.graceMs > 0) await this.sleep(this.deps.graceMs);
    if (isOwnSignature(tx.signature)) return;
    if (readAllowListedSignatures(this.deps.allowListPath).has(tx.signature)) return;
    this.foreign.push(tx.signature);
    this.log.error('wallet tx signed outside this process', { signature: tx.signature, slot: tx.slot });
    this.deps.bus.emit('alert', {
      level: 'error',
      message: `🚨 foreign signer: wallet ${short(this.deps.wallet)} paid tx ${short(tx.signature)} that this bot never signed — another instance or a leaked key`,
      telegram: true,
    });
    if (!this.tripped) {
      this.tripped = true;
      this.deps.halt(`foreign signer on the trading wallet (tx ${short(tx.signature)})`);
    }
  }

  /**
   * Startup audit: recent wallet signatures the DB has never heard of. Alert
   * only — a ledger reset or a manual operator action also produces these.
   */
  async auditRecent(rpc: Pick<RpcClient, 'getSignaturesForAddress'>, known: (signature: string) => boolean, limit: number): Promise<string[]> {
    if (limit <= 0) return [];
    try {
      const recent = await rpc.getSignaturesForAddress(this.deps.wallet, { limit });
      const allow = readAllowListedSignatures(this.deps.allowListPath);
      const unknown = recent.map((r) => r.signature).filter((s) => !known(s) && !allow.has(s));
      if (unknown.length > 0) {
        this.log.warn('wallet has recent transactions this bot has no record of', { count: unknown.length, sample: unknown.slice(0, 5) });
        this.deps.bus.emit('alert', {
          level: 'warn',
          message: `⚠ ${unknown.length}/${recent.length} recent wallet txs are not in this bot's records — check for another instance (e.g. ${short(unknown[0]!)})`,
          telegram: true,
        });
      }
      return unknown;
    } catch (err) {
      this.log.warn('startup wallet audit failed', { err });
      return [];
    }
  }
}

function short(a: string): string {
  return `${a.slice(0, 4)}…${a.slice(-4)}`;
}
