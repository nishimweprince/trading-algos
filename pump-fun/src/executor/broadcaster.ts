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
  send(txBytes: Uint8Array): Promise<TxSendResult>;
}

export interface TxSendResult {
  signature: string;
  bundleId?: string;
}

export interface TxSendAttempt {
  route: string;
  submittedAtMs: number;
  sent: boolean;
  signature?: string | undefined;
  bundleId?: string | undefined;
  sendErr?: string | undefined;
}

export interface ConfirmationResult {
  confirmationStatus: 'processed' | 'confirmed' | 'finalized' | null;
  slot?: number | undefined;
  err?: unknown;
}

export interface BroadcastResult {
  mode: RunMode;
  simulated: boolean;
  sent: boolean;
  confirmed: boolean;
  route?: string | undefined;
  signature?: string | undefined;
  confirmationStatus?: 'processed' | 'confirmed' | 'finalized' | null | undefined;
  slot?: number | undefined;
  submittedAtMs?: number | undefined;
  confirmedAtMs?: number | undefined;
  confirmLatencyMs?: number | undefined;
  bundleId?: string | undefined;
  simErr?: unknown;
  sendErr?: unknown;
  logs?: string[];
  landedVia?: string;
  attempts: TxSendAttempt[];
}

export class BroadcastError extends Error {
  override name = 'BroadcastError';
}

export class Broadcaster {
  private readonly mode: RunMode;
  private readonly senders: TxSender[];
  private readonly simulator: TxSender | undefined;
  private readonly confirmSignature: ((signature: string) => Promise<ConfirmationResult | null>) | undefined;
  private readonly confirmTimeoutMs: number;
  private readonly confirmPollMs: number;
  private readonly log = logger.child({ mod: 'broadcaster' });

  constructor(
    mode: RunMode,
    senders: TxSender[],
    opts: {
      simulator?: TxSender;
      confirmSignature?: (signature: string) => Promise<ConfirmationResult | null>;
      confirmTimeoutMs?: number;
      confirmPollMs?: number;
    } = {},
  ) {
    this.mode = mode;
    this.senders = senders;
    this.simulator = opts.simulator;
    this.confirmSignature = opts.confirmSignature;
    this.confirmTimeoutMs = opts.confirmTimeoutMs ?? 12_000;
    this.confirmPollMs = opts.confirmPollMs ?? 500;
  }

  async broadcast(
    txBytes: Uint8Array,
    label: string,
    opts: { skipSimulation?: boolean; confirmTimeoutMs?: number; confirmPollMs?: number } = {},
  ): Promise<BroadcastResult> {
    // Hard guard: reaching the broadcaster in paper mode is a bug — paper never
    // builds or signs a transaction. Fail loudly rather than risk a send.
    if (this.mode === 'paper') {
      throw new BroadcastError(`broadcaster invoked in paper mode (${label}) — no transaction may exist in paper`);
    }
    if (this.senders.length === 0) {
      throw new BroadcastError('no send paths configured');
    }

    // Dry-run must ALWAYS simulate (that is the whole point of dry-run) — the
    // skip only applies to live pre-signed exits where speed matters and the tx
    // was already validated at build time.
    let logs: string[] = [];
    const simulated = this.mode === 'dry-run' || !opts.skipSimulation;
    if (simulated) {
      const sim = await this.simulate(txBytes);
      logs = sim.logs;
      if (this.mode === 'dry-run') {
        this.log.info('dry-run: simulated, NOT sent', { label, ok: !sim.err });
        return { mode: this.mode, simulated: true, sent: false, confirmed: false, simErr: sim.err, logs: sim.logs, attempts: [] };
      }
      // live: refuse to send a transaction that fails simulation.
      if (sim.err) {
        this.log.warn('live: simulation failed — not sending', { label, err: sim.err });
        return { mode: this.mode, simulated: true, sent: false, confirmed: false, simErr: sim.err, logs: sim.logs, attempts: [] };
      }
    }

    const attempts = await Promise.all(this.senders.map((s) => this.sendVia(s, txBytes)));
    const firstSent = attempts.find((a) => a.sent && a.signature);
    if (!firstSent?.signature) {
      const reason = attempts.find((a) => a.sendErr)?.sendErr ?? 'unknown';
      throw new BroadcastError(`all ${this.senders.length} send paths failed (${label}): ${reason}`);
    }

    if (!this.confirmSignature) {
      this.log.info('live: sent (no confirmer configured)', { label, via: firstSent.route, signature: firstSent.signature });
      return {
        mode: this.mode,
        simulated,
        sent: true,
        confirmed: true,
        route: firstSent.route,
        signature: firstSent.signature,
        bundleId: firstSent.bundleId,
        landedVia: firstSent.route,
        submittedAtMs: firstSent.submittedAtMs,
        confirmedAtMs: Date.now(),
        confirmLatencyMs: 0,
        confirmationStatus: 'confirmed',
        logs,
        attempts,
      };
    }

    const confirmed = await this.waitForConfirmation(
      firstSent.signature,
      firstSent.submittedAtMs,
      opts.confirmTimeoutMs,
      opts.confirmPollMs,
    );
    if (!confirmed.confirmed) {
      this.log.warn('live: sent but not confirmed', { label, signature: firstSent.signature, err: confirmed.err });
      return {
        mode: this.mode,
        simulated,
        sent: true,
        confirmed: false,
        route: firstSent.route,
        signature: firstSent.signature,
        bundleId: firstSent.bundleId,
        landedVia: firstSent.route,
        submittedAtMs: firstSent.submittedAtMs,
        confirmationStatus: confirmed.status?.confirmationStatus,
        slot: confirmed.status?.slot,
        sendErr: confirmed.err,
        logs,
        attempts,
      };
    }

    this.log.info('live: confirmed', { label, via: firstSent.route, signature: firstSent.signature, slot: confirmed.status?.slot });
    return {
      mode: this.mode,
      simulated,
      sent: true,
      confirmed: true,
      route: firstSent.route,
      signature: firstSent.signature,
      bundleId: firstSent.bundleId,
      landedVia: firstSent.route,
      submittedAtMs: firstSent.submittedAtMs,
      confirmedAtMs: confirmed.confirmedAtMs,
      confirmLatencyMs: confirmed.confirmedAtMs - firstSent.submittedAtMs,
      confirmationStatus: confirmed.status?.confirmationStatus,
      slot: confirmed.status?.slot,
      logs,
      attempts,
    };
  }

  private async simulate(txBytes: Uint8Array): Promise<{ err: unknown; logs: string[] }> {
    const simulator = this.simulator ?? this.senders[0];
    if (!simulator) throw new BroadcastError('no simulation path configured');
    return simulator.simulate(txBytes);
  }

  private async sendVia(sender: TxSender, txBytes: Uint8Array): Promise<TxSendAttempt> {
    const submittedAtMs = Date.now();
    try {
      const res = await sender.send(txBytes);
      return {
        route: sender.name,
        submittedAtMs,
        sent: true,
        signature: res.signature,
        ...(res.bundleId ? { bundleId: res.bundleId } : {}),
      };
    } catch (err) {
      return { route: sender.name, submittedAtMs, sent: false, sendErr: (err as Error).message };
    }
  }

  private async waitForConfirmation(
    signature: string,
    submittedAtMs: number,
    timeoutMs = this.confirmTimeoutMs,
    pollMs = this.confirmPollMs,
  ): Promise<{ confirmed: boolean; confirmedAtMs: number; status: ConfirmationResult | null; err?: unknown }> {
    const deadline = submittedAtMs + timeoutMs;
    let last: ConfirmationResult | null = null;
    while (Date.now() <= deadline) {
      last = await this.confirmSignature!(signature);
      if (last?.err) return { confirmed: false, confirmedAtMs: Date.now(), status: last, err: last.err };
      if (last?.confirmationStatus === 'confirmed' || last?.confirmationStatus === 'finalized') {
        return { confirmed: true, confirmedAtMs: Date.now(), status: last };
      }
      await delay(pollMs);
    }
    return { confirmed: false, confirmedAtMs: Date.now(), status: last, err: 'confirmation timeout' };
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
