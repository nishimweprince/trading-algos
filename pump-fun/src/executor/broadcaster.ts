import type { RunMode } from '../config/schema.ts';
import type { SlotClock } from '../core/slotClock.ts';
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
  simulate(txBytes: Uint8Array): Promise<{ err: unknown; logs: string[]; unitsConsumed?: number | undefined }>;
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
  /** Slot the transaction landed in (from the confirmation status). */
  slot?: number | undefined;
  /** SlotClock reading when the sends were dispatched (chain-relative). */
  submittedSlot?: number | undefined;
  /** slot − submittedSlot: how many slots inclusion took. */
  slotsToLand?: number | undefined;
  submittedAtMs?: number | undefined;
  confirmedAtMs?: number | undefined;
  confirmLatencyMs?: number | undefined;
  bundleId?: string | undefined;
  /** Compute units the pre-send simulation consumed (P4.1 CU tuning). */
  unitsConsumed?: number | undefined;
  /** Every route that accepted the send (all carry the same signature). */
  acceptedVia?: string[] | undefined;
  /** Mid-price move (%) from the verdict's pool snapshot to the buy quote's state read. */
  entryMovePct?: number | undefined;
  simErr?: unknown;
  sendErr?: unknown;
  logs?: string[];
  landedVia?: string;
  /**
   * Sent, but the confirm window ran out with no on-chain error: the tx may
   * still land. Callers must resolve it against the chain, never treat it as failed.
   */
  landingUnknown?: boolean | undefined;
  attempts: TxSendAttempt[];
}

/** sendErr of a sent tx whose confirmation window ran out with no on-chain error. */
export const CONFIRM_TIMEOUT = 'confirmation timeout';

/** How long a broadcast result waits for straggler routes before snapshotting attempts. */
const SEND_STRAGGLER_MS = 200;

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
  private readonly slotClock: SlotClock | undefined;
  private readonly log = logger.child({ mod: 'broadcaster' });

  constructor(
    mode: RunMode,
    senders: TxSender[],
    opts: {
      simulator?: TxSender;
      confirmSignature?: (signature: string) => Promise<ConfirmationResult | null>;
      confirmTimeoutMs?: number;
      confirmPollMs?: number;
      /** Optional: stamps submittedSlot / slotsToLand on results. */
      slotClock?: SlotClock | undefined;
    } = {},
  ) {
    this.mode = mode;
    this.senders = senders;
    this.simulator = opts.simulator;
    this.confirmSignature = opts.confirmSignature;
    this.confirmTimeoutMs = opts.confirmTimeoutMs ?? 12_000;
    this.confirmPollMs = opts.confirmPollMs ?? 500;
    this.slotClock = opts.slotClock;
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
    let unitsConsumed: number | undefined;
    const simulated = this.mode === 'dry-run' || !opts.skipSimulation;
    if (simulated) {
      const sim = await this.simulate(txBytes);
      logs = sim.logs;
      unitsConsumed = sim.unitsConsumed;
      if (this.mode === 'dry-run') {
        this.log.info('dry-run: simulated, NOT sent', { label, ok: !sim.err });
        return { mode: this.mode, simulated: true, sent: false, confirmed: false, simErr: sim.err, logs: sim.logs, unitsConsumed: sim.unitsConsumed, attempts: [] };
      }
      // live: refuse to send a transaction that fails simulation.
      if (sim.err) {
        this.log.warn('live: simulation failed — not sending', { label, err: sim.err });
        return { mode: this.mode, simulated: true, sent: false, confirmed: false, simErr: sim.err, logs: sim.logs, attempts: [] };
      }
    }

    // The slot reading runs concurrently with the sends: a push reading is
    // synchronous, and the getSlot fallback must never delay dispatch.
    //
    // Confirmation starts on the FIRST route's ack, not after the slowest one:
    // awaiting every route used to spend the whole confirm budget on a slow
    // secondary path, so the deadline expired with zero polls and a buy that
    // later landed was reported unconfirmed (2026-10-03 wallet drain).
    const settled: TxSendAttempt[] = [];
    const sends = this.senders.map((s) =>
      this.sendVia(s, txBytes).then((a) => {
        settled.push(a);
        return a;
      }),
    );
    const [firstSent, submittedSlot] = await Promise.all([firstAccepted(sends), this.readSubmittedSlot()]);
    if (!firstSent?.signature) {
      const attempts = await Promise.all(sends);
      const reason = attempts.find((a) => a.sendErr)?.sendErr ?? 'unknown';
      throw new BroadcastError(`all ${this.senders.length} send paths failed (${label}): ${reason}`);
    }
    const ackAtMs = Date.now();
    // Snapshot of the routes after confirmation: give stragglers a moment, never block on them.
    const collect = async (): Promise<{ attempts: TxSendAttempt[]; acceptedVia: string[] }> => {
      await Promise.race([Promise.all(sends), delay(SEND_STRAGGLER_MS)]);
      const attempts = this.senders
        .map((s) => settled.find((a) => a.route === s.name))
        .filter((a): a is TxSendAttempt => a !== undefined);
      // Identical signed bytes go down every path, so all routes share one
      // signature and the landing route is unknowable from the signature alone;
      // record every route that accepted it (and each ack latency in attempts).
      return { attempts, acceptedVia: attempts.filter((a) => a.sent).map((a) => a.route) };
    };

    if (!this.confirmSignature) {
      const { attempts, acceptedVia } = await collect();
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
        acceptedVia,
        unitsConsumed,
        submittedAtMs: firstSent.submittedAtMs,
        submittedSlot,
        confirmedAtMs: Date.now(),
        confirmLatencyMs: 0,
        confirmationStatus: 'confirmed',
        logs,
        attempts,
      };
    }

    const confirmed = await this.waitForConfirmation(
      firstSent.signature,
      ackAtMs,
      opts.confirmTimeoutMs,
      opts.confirmPollMs,
    );
    const { attempts, acceptedVia } = await collect();
    if (!confirmed.confirmed) {
      // A timeout (no on-chain error seen) means the tx may still land.
      const landingUnknown = confirmed.err === CONFIRM_TIMEOUT;
      this.log.warn('live: sent but not confirmed', { label, signature: firstSent.signature, err: confirmed.err, landingUnknown });
      return {
        landingUnknown,
        mode: this.mode,
        simulated,
        sent: true,
        confirmed: false,
        route: firstSent.route,
        signature: firstSent.signature,
        bundleId: firstSent.bundleId,
        landedVia: firstSent.route,
        acceptedVia,
        unitsConsumed,
        submittedAtMs: firstSent.submittedAtMs,
        submittedSlot,
        confirmationStatus: confirmed.status?.confirmationStatus,
        slot: confirmed.status?.slot,
        slotsToLand: slotsToLand(submittedSlot, confirmed.status?.slot),
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
      submittedSlot,
      slotsToLand: slotsToLand(submittedSlot, confirmed.status?.slot),
      acceptedVia,
      logs,
      attempts,
    };
  }

  /**
   * Simulate without sending. Used by the parallel buy path, which simulates
   * every slippage tier concurrently and then sends only the tightest one that
   * passed — instead of discovering a 6004 one serial round at a time.
   *
   * Simulation is read-only and risks no funds, so running several at once is
   * safe in a way that running several SENDS would not be (both would land).
   */
  async simulateOnly(txBytes: Uint8Array): Promise<{ err: unknown; logs: string[]; unitsConsumed?: number | undefined }> {
    return this.simulate(txBytes);
  }

  private async simulate(txBytes: Uint8Array): Promise<{ err: unknown; logs: string[]; unitsConsumed?: number | undefined }> {
    const simulator = this.simulator ?? this.senders[0];
    if (!simulator) throw new BroadcastError('no simulation path configured');
    return simulator.simulate(txBytes);
  }

  /** Never throws; undefined without a clock or reading. */
  private async readSubmittedSlot(): Promise<number | undefined> {
    if (!this.slotClock) return undefined;
    return this.slotClock.get()?.slot ?? this.slotClock.current();
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
    startedAtMs: number,
    timeoutMs = this.confirmTimeoutMs,
    pollMs = this.confirmPollMs,
  ): Promise<{ confirmed: boolean; confirmedAtMs: number; status: ConfirmationResult | null; err?: unknown }> {
    const deadline = startedAtMs + timeoutMs;
    let last: ConfirmationResult | null = null;
    // Always poll at least once, and never let a status-read error escape:
    // the tx is already out, so a throw here would drop a buy that may land.
    do {
      try {
        last = await this.confirmSignature!(signature);
      } catch (err) {
        this.log.debug('confirm poll failed — retrying', { signature, err });
      }
      if (last?.err) return { confirmed: false, confirmedAtMs: Date.now(), status: last, err: last.err };
      if (last?.confirmationStatus === 'confirmed' || last?.confirmationStatus === 'finalized') {
        return { confirmed: true, confirmedAtMs: Date.now(), status: last };
      }
      if (Date.now() + pollMs > deadline) break;
      await delay(pollMs);
    } while (Date.now() <= deadline);
    return { confirmed: false, confirmedAtMs: Date.now(), status: last, err: CONFIRM_TIMEOUT };
  }
}

/** Resolves with the first attempt that was accepted, or undefined once every route has failed. */
function firstAccepted(sends: Promise<TxSendAttempt>[]): Promise<TxSendAttempt | undefined> {
  return new Promise((resolve) => {
    let pending = sends.length;
    if (pending === 0) resolve(undefined);
    for (const p of sends) {
      void p.then((a) => {
        if (a.sent && a.signature) resolve(a);
        else if (--pending === 0) resolve(undefined);
      });
    }
  });
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function slotsToLand(submittedSlot: number | undefined, landedSlot: number | undefined): number | undefined {
  return submittedSlot !== undefined && landedSlot !== undefined ? landedSlot - submittedSlot : undefined;
}
