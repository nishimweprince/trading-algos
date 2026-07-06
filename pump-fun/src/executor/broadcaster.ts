import type { RunMode } from '../config/schema.ts';
import { logger } from '../core/logger.ts';

/**
 * Broadcaster — the ONE place run-mode gating lives (Section 11). This is the
 * safety keystone: no code path may send a real transaction in paper or dry-run
 * mode, and this invariant is enforced here and covered by tests.
 *
 *   paper    → never invoked; if it is, that's a bug and we throw.
 *   dry-run  → simulate on one path, NEVER send.
 *   live     → simulate once, then send on every configured path (multi-path
 *              broadcast); first confirmation wins.
 *
 * The transaction is passed as raw signed bytes; senders are injected so the
 * gating logic is unit-testable without a network.
 */

export interface TxSender {
  readonly name: string;
  simulate(txBytes: Uint8Array): Promise<{ err: unknown; logs: string[] }>;
  send(txBytes: Uint8Array): Promise<string>;
}

export interface BroadcastResult {
  mode: RunMode;
  simulated: boolean;
  sent: boolean;
  signature?: string;
  simErr?: unknown;
  logs?: string[];
  landedVia?: string;
}

export class BroadcastError extends Error {
  override name = 'BroadcastError';
}

export class Broadcaster {
  private readonly mode: RunMode;
  private readonly senders: TxSender[];
  private readonly log = logger.child({ mod: 'broadcaster' });

  constructor(mode: RunMode, senders: TxSender[]) {
    this.mode = mode;
    this.senders = senders;
  }

  async broadcast(txBytes: Uint8Array, label: string): Promise<BroadcastResult> {
    // Hard guard: reaching the broadcaster in paper mode is a bug — paper never
    // builds or signs a transaction. Fail loudly rather than risk a send.
    if (this.mode === 'paper') {
      throw new BroadcastError(`broadcaster invoked in paper mode (${label}) — no transaction may exist in paper`);
    }
    if (this.senders.length === 0) {
      throw new BroadcastError('no send paths configured');
    }

    // Always simulate first.
    const sim = await this.senders[0]!.simulate(txBytes);
    if (this.mode === 'dry-run') {
      this.log.info('dry-run: simulated, NOT sent', { label, ok: !sim.err });
      return { mode: this.mode, simulated: true, sent: false, simErr: sim.err, logs: sim.logs };
    }

    // live: refuse to send a transaction that fails simulation.
    if (sim.err) {
      this.log.warn('live: simulation failed — not sending', { label, err: sim.err });
      return { mode: this.mode, simulated: true, sent: false, simErr: sim.err, logs: sim.logs };
    }

    // Multi-path send; first success wins.
    const results = await Promise.allSettled(this.senders.map((s) => s.send(txBytes).then((sig) => ({ sig, via: s.name }))));
    for (const r of results) {
      if (r.status === 'fulfilled') {
        this.log.info('live: sent', { label, via: r.value.via, signature: r.value.sig });
        return { mode: this.mode, simulated: true, sent: true, signature: r.value.sig, landedVia: r.value.via, logs: sim.logs };
      }
    }
    const reason = results[0]?.status === 'rejected' ? String(results[0].reason) : 'unknown';
    throw new BroadcastError(`all ${this.senders.length} send paths failed (${label}): ${reason}`);
  }
}
