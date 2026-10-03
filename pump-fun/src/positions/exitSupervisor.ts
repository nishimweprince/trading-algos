import type { Config } from '../config/schema.ts';
import type { TypedBus } from '../core/bus.ts';
import type { ExitTrigger, Mint, PoolPricingRef, Position } from '../core/types.ts';
import type { BroadcastResult } from '../executor/broadcaster.ts';
import type { Executor } from '../executor/index.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { ExitLadder } from './presign.ts';
import type { Fill } from './position.ts';
import { logger } from '../core/logger.ts';
import type { FillActuals } from '../executor/fillActuals.ts';

export interface ExitAttemptRecord {
  route?: string | undefined;
  signature?: string | undefined;
  bundleId?: string | undefined;
  confirmed: boolean;
  slippagePct: number;
  atMs: number;
  error?: string | undefined;
}

export interface LiveExitIntent {
  mint: Mint;
  trigger: ExitTrigger;
  poolAddress: string;
  baseMint: string;
  baseIsToken2022: boolean;
  targetRawAmount: string;
  remainingRawAmount: string;
  originalRawAmount: string;
  fullRemainder: boolean;
  createdAtMs: number;
  attempts: ExitAttemptRecord[];
  nextRetryAtMs: number;
  lastError?: string | undefined;
  status: 'pending' | 'retrying' | 'confirmed' | 'critical';
  /** Re-armed after an earlier exit went critical: every attempt uses the emergency bound. */
  escalated?: boolean | undefined;
}

export interface ExitSupervisorDeps {
  config: Config;
  bus: TypedBus;
  repos: Repositories;
  executor: Executor;
  now?: () => number;
}

export interface StartExitArgs {
  position: Position;
  pricing: PoolPricingRef;
  fill: Fill;
  fullRemainder: boolean;
  rawBaseAmount: bigint;
  originalRawBaseAmount: bigint;
  ladder?: ExitLadder | undefined;
  entryTx?: string | undefined;
  executionJson?: string | undefined;
  momentumWindowMs?: number | undefined;
  /** Skip the ordinary tiers (they already failed on an earlier exit). */
  escalated?: boolean | undefined;
  /**
   * Latest pool reserves for this position (the triggering tick, then any
   * newer one on a retry). Lets the executor build the sell at trigger time
   * against the live price with no network round trip.
   */
  liveReserves?: (() => { baseReserve: bigint; quoteReserveLamports: bigint } | null) | undefined;
  /** Resolves when our token account's push shows a balance below `raw` (the sell landed). */
  awaitBalanceBelow?: ((raw: bigint) => Promise<void>) | undefined;
  /** Latest pushed balance of our token account, if the stream has reported one. */
  currentBalance?: (() => bigint | undefined) | undefined;
}

export interface ExitOutcome {
  confirmed: boolean;
  intent: LiveExitIntent;
  result?: BroadcastResult | undefined;
  remainingRawAmount: bigint;
  exitTx?: string | undefined;
  exitTriggerToConfirmMs?: number | undefined;
  /**
   * The landed sell tx read from the chain (SOL received, tokens sold). When
   * present the wallet ledger is credited with it directly — no modelled
   * credit, no later correction for this tx.
   */
  actual?: FillActuals | undefined;
  /** Wall clock when the landing was established (for exit_landed_to_credited). */
  landedAtMs?: number | undefined;
}

export class ExitSupervisor {
  private readonly config: Config;
  private readonly bus: TypedBus;
  private readonly repos: Repositories;
  private readonly executor: Executor;
  private readonly now: () => number;
  private readonly log = logger.child({ mod: 'exit-supervisor' });

  constructor(deps: ExitSupervisorDeps) {
    this.config = deps.config;
    this.bus = deps.bus;
    this.repos = deps.repos;
    this.executor = deps.executor;
    this.now = deps.now ?? (() => Date.now());
  }

  async startExit(args: StartExitArgs): Promise<ExitOutcome> {
    const intent: LiveExitIntent = {
      mint: args.position.mint,
      trigger: args.fill.trigger,
      poolAddress: args.pricing.poolAddress,
      baseMint: args.pricing.baseMint,
      baseIsToken2022: args.pricing.baseIsToken2022 ?? false,
      targetRawAmount: this.rawTarget(args).toString(),
      remainingRawAmount: args.rawBaseAmount.toString(),
      originalRawAmount: args.originalRawBaseAmount.toString(),
      fullRemainder: args.fullRemainder,
      createdAtMs: this.now(),
      attempts: [],
      nextRetryAtMs: this.now(),
      status: 'pending',
      ...(args.escalated ? { escalated: true } : {}),
    };
    this.persistIntent(args.position, args, intent);
    return this.runUntilResolved(args, intent);
  }

  async recoverExit(args: StartExitArgs, intent: LiveExitIntent): Promise<ExitOutcome> {
    const remaining = await this.executor.reconcileTokenBalance(args.pricing.baseMint, args.pricing.baseIsToken2022 ?? false);
    if (remaining <= 0n) {
      intent.status = 'confirmed';
      intent.remainingRawAmount = '0';
      this.persistIntent(args.position, args, intent);
      return { confirmed: true, intent, remainingRawAmount: 0n };
    }
    intent.remainingRawAmount = remaining.toString();
    intent.status = 'retrying';
    this.persistIntent(args.position, args, intent);
    return this.runUntilResolved({ ...args, rawBaseAmount: remaining }, intent);
  }

  private async runUntilResolved(args: StartExitArgs, intent: LiveExitIntent): Promise<ExitOutcome> {
    let remaining = BigInt(intent.remainingRawAmount);
    let lastResult: BroadcastResult | undefined;
    let exitTx: string | undefined;
    const startedAtMs = this.now();
    const t22 = args.pricing.baseIsToken2022 ?? false;

    while (intent.attempts.length < this.config.exits.maxExitAttempts) {
      const waitMs = Math.max(0, intent.nextRetryAtMs - this.now());
      if (waitMs > 0) await delay(waitMs);

      const slippagePct = this.slippageForAttempt(intent);
      // Escalate straight away unless a sent sell may still land: nothing is
      // pending after a refused send or an on-chain failure, and waiting only
      // holds a losing position open.
      let pending = false;
      try {
        const result = await this.broadcastAttempt(args, intent, slippagePct);
        lastResult = result;
        exitTx = result.signature;
        if (intent.attempts.length === 0 && result.submittedAtMs !== undefined) {
          this.recordSample('exit_trigger_to_send', result.submittedAtMs - intent.createdAtMs, args.position.mint);
        }
        intent.attempts.push({
          route: result.route,
          signature: result.signature,
          bundleId: result.bundleId,
          confirmed: result.confirmed,
          slippagePct,
          atMs: this.now(),
          ...(result.sendErr ? { error: String(result.sendErr) } : {}),
          ...(result.simErr ? { error: `sim: ${String(JSON.stringify(result.simErr))}` } : {}),
        });

        const before = BigInt(intent.remainingRawAmount);
        let actual: FillActuals | null = null;
        const atProcessed = result.confirmed && result.confirmationStatus === 'processed';
        if (result.sent && atProcessed) {
          // Landed at processed (status or account push): don't wait for the
          // confirmed tx — the remainder is the pushed balance (or one read);
          // proceeds are estimated now and corrected from actuals right after.
          const pushed = args.currentBalance?.();
          remaining = pushed !== undefined && pushed < before ? pushed : await this.readBalanceOnce(args.pricing.baseMint, t22, before);
          if (remaining >= before && !intent.fullRemainder) remaining = before - BigInt(intent.targetRawAmount) > 0n ? before - BigInt(intent.targetRawAmount) : 0n;
          if (remaining >= before && intent.fullRemainder) remaining = 0n;
        } else if (result.sent && result.confirmed && result.signature) {
          // Reconcile FIRST, from the landed sell itself: one getTransaction
          // gives the SOL received and the tokens sold. Replaces the old
          // post-sell balance loop that slept ~1.2 s whenever it read 0.
          actual = await this.readActual(result.signature, args.pricing.baseMint, t22);
          if (actual) {
            remaining = before + actual.tokenRawDelta;
            if (remaining < 0n) remaining = 0n;
          } else {
            // Confirmed with no error means it sold: if even the balance read
            // fails, assume the expected remainder rather than re-selling.
            const target = BigInt(intent.targetRawAmount);
            const expected = intent.fullRemainder ? 0n : before > target ? before - target : 0n;
            remaining = await this.readBalanceOnce(args.pricing.baseMint, t22, expected);
          }
        } else if (result.sent && result.landingUnknown) {
          // May land late: one balance read decides; if still unsold, wait for it.
          remaining = await this.readBalanceOnce(args.pricing.baseMint, t22, before);
          pending = remaining >= before;
        } else {
          remaining = before; // refused or failed on-chain: nothing was sold
        }
        intent.remainingRawAmount = remaining.toString();
        const landed = result.confirmed || remaining < before;

        if (landed && (!intent.fullRemainder || remaining <= 0n)) {
          const landedAtMs = result.confirmedAtMs ?? this.now();
          intent.status = 'confirmed';
          if (result.submittedAtMs !== undefined) {
            this.recordSample('exit_send_to_landed', landedAtMs - result.submittedAtMs, args.position.mint);
          }
          this.persistIntent(args.position, args, intent, result);
          this.recordLandSlots(args.position.mint, result);
          return {
            confirmed: true,
            intent,
            result,
            remainingRawAmount: remaining,
            ...(exitTx ? { exitTx } : {}),
            ...confirmMs(result, startedAtMs),
            ...(actual ? { actual } : {}),
            landedAtMs,
          };
        }

        intent.status = 'retrying';
        intent.lastError = result.confirmed
          ? `confirmed but token balance remains ${remaining.toString()}`
          : !result.sent
            ? `sell refused: ${String(JSON.stringify(result.simErr ?? 'not sent'))}`
            : `unconfirmed sell: ${String(JSON.stringify(result.sendErr ?? 'unknown'))}`;
      } catch (err) {
        intent.status = 'retrying';
        intent.lastError = (err as Error).message;
        intent.attempts.push({ confirmed: false, slippagePct, atMs: this.now(), error: intent.lastError });
      }

      intent.nextRetryAtMs = pending ? this.now() + this.config.exits.exitRetryMs : this.now();
      this.persistIntent(args.position, args, intent, lastResult);
    }

    intent.status = 'critical';
    intent.lastError = intent.lastError ?? 'max exit attempts exhausted';
    this.persistIntent(args.position, args, intent, lastResult);
    this.bus.emit('alert', {
      level: 'error',
      message: `CRITICAL live exit unresolved ${short(intent.mint)} — ${intent.lastError}`,
      telegram: true,
    });
    this.bus.emit('killSwitch', { source: 'internal', detail: `live exit unresolved for ${intent.mint}` });
    return {
      confirmed: false,
      intent,
      ...(lastResult ? { result: lastResult } : {}),
      remainingRawAmount: remaining,
      ...(exitTx ? { exitTx } : {}),
    };
  }

  /** The landed sell's on-chain actuals, within the exits.exitActuals* budget; null if not readable. */
  private async readActual(signature: string, baseMint: string, t22: boolean): Promise<FillActuals | null> {
    if (typeof this.executor.fillActuals !== 'function') return null;
    try {
      return await this.executor.fillActuals(signature, baseMint, t22, {
        attempts: this.config.exits.exitActualsAttempts,
        delayMs: this.config.exits.exitActualsDelayMs,
      });
    } catch (err) {
      this.log.debug('exit actuals read failed — falling back to one balance read', { signature, err });
      return null;
    }
  }

  /** ONE token-balance read (no retry-on-zero: after a sell, zero is the answer). */
  private async readBalanceOnce(baseMint: string, t22: boolean, fallback: bigint): Promise<bigint> {
    try {
      return await this.executor.readTokenBalance(baseMint, t22);
    } catch (err) {
      this.log.debug('exit balance read failed — assuming unchanged', { baseMint, err });
      return fallback;
    }
  }

  private recordSample(kind: 'exit_trigger_to_send' | 'exit_send_to_landed', ms: number, mint: string): void {
    if (!Number.isFinite(ms) || ms < 0) return;
    try {
      this.repos.recordLatencySample({ kind, latencyMs: ms, mint });
    } catch (err) {
      this.log.debug('exit latency sample failed', { kind, err });
    }
  }

  private async broadcastAttempt(args: StartExitArgs, intent: LiveExitIntent, slippagePct: number): Promise<BroadcastResult> {
    const raw = BigInt(intent.remainingRawAmount) < BigInt(intent.targetRawAmount)
      ? BigInt(intent.remainingRawAmount)
      : BigInt(intent.targetRawAmount);
    if (raw <= 0n) throw new Error('nothing to sell');
    // 1. Trigger-time build: cached state + live reserves, no RPC before the send.
    // Wakes the confirm wait the moment our token account's push shows the sale.
    const landed = args.awaitBalanceBelow?.(BigInt(intent.remainingRawAmount));
    const reserves = args.liveReserves?.() ?? null;
    if (reserves && typeof this.executor.buildExitTx === 'function') {
      try {
        const bytes = await this.executor.buildExitTx(intent.baseMint, raw, slippagePct, reserves);
        if (bytes) return this.executor.broadcastSignedExit(bytes, intent.baseMint, landed);
      } catch (err) {
        this.log.warn('trigger-time exit build failed — falling back', { mint: intent.mint, err });
      }
    }
    // 2. Pre-signed ladder (quoted at its last refresh).
    if (intent.fullRemainder && args.ladder && !args.ladder.isStale(this.config.exits.ladderRefreshMs)) {
      const tier = slippagePct >= this.emergencySlippage() ? args.ladder.emergency() : args.ladder.pick(slippagePct);
      if (tier) return this.executor.broadcastSignedExit(tier.bytes, intent.baseMint, landed);
    }
    // 3. Fresh build (one state read).
    const opts = typeof this.executor.exitSendOpts === 'function' ? this.executor.exitSendOpts(landed) : undefined;
    return this.executor.sellAndConfirm(intent.poolAddress, intent.baseMint, raw, slippagePct, opts);
  }

  /**
   * Slippage for the next attempt, measured against the live price:
   *   EMERGENCY_EXIT / KILL_SWITCH / re-armed after critical -> emergency bound
   *   take-profit (incl. partial TP legs)                     -> takeProfitSlippageTiers
   *   any other full exit (stop, trail, time, blind)          -> protectiveSlippageTiers
   * Each failed attempt moves one tier out; the last tier repeats.
   */
  private slippageForAttempt(intent: LiveExitIntent): number {
    if (intent.escalated || intent.trigger === 'EMERGENCY_EXIT' || intent.trigger === 'KILL_SWITCH') {
      return this.emergencySlippage();
    }
    const isTp = intent.trigger.startsWith('TAKE_PROFIT') || !intent.fullRemainder;
    const tiers = isTp ? this.config.exits.takeProfitSlippageTiers : this.config.exits.protectiveSlippageTiers;
    return tiers[Math.min(intent.attempts.length, tiers.length - 1)] ?? this.emergencySlippage();
  }

  private emergencySlippage(): number {
    const tiers = this.config.exits.ladderSlippageTiers;
    const worstOrdinary = tiers[tiers.length - 1] ?? this.config.entry.maxSlippagePct;
    return Math.max(this.config.exits.emergencySlippagePct, worstOrdinary);
  }

  private rawTarget(args: StartExitArgs): bigint {
    if (args.fullRemainder) return args.rawBaseAmount;
    const scaled = BigInt(Math.floor(args.fill.fraction * 1_000_000));
    const target = (args.originalRawBaseAmount * scaled) / 1_000_000n;
    return target < args.rawBaseAmount ? target : args.rawBaseAmount;
  }

  /** Chain-relative exit inclusion latency (slots between dispatch and landing). */
  private recordLandSlots(mint: string, result: BroadcastResult): void {
    if (result.slotsToLand === undefined) return;
    const route = result.landedVia ?? result.route;
    try {
      this.repos.recordLatencySample({
        kind: 'exit_land_slots',
        latencyMs: result.slotsToLand,
        mint,
        ...(route ? { feedSource: route } : {}),
      });
    } catch (err) {
      this.log.debug('exit land-slot sample failed', { mint, err });
    }
  }

  private persistIntent(
    p: Position,
    args: StartExitArgs,
    intent: LiveExitIntent,
    result?: BroadcastResult,
  ): void {
    this.repos.upsertPosition({
      mint: p.mint,
      state: 'EXITING',
      sizeSol: p.sizeSol,
      ...(p.entryPrice !== undefined ? { entryPrice: p.entryPrice } : {}),
      ...(p.openedAt !== undefined ? { openedAt: p.openedAt } : {}),
      exitTrigger: intent.trigger,
    }, {
      entryTx: args.entryTx,
      exitTx: result?.signature,
      rawBaseAmount: BigInt(intent.remainingRawAmount),
      pricingJson: safeJson(args.pricing),
      executionJson: safeJson({ previous: args.executionJson, lastExit: result }),
      exitIntentJson: safeJson(intent),
      exitTriggerToConfirmMs: result?.confirmLatencyMs,
      momentumWindowMs: args.momentumWindowMs,
    });
  }
}

export function parseExitIntent(json: string): LiveExitIntent {
  const parsed = JSON.parse(json) as LiveExitIntent;
  if (!parsed || typeof parsed !== 'object' || typeof parsed.mint !== 'string' || typeof parsed.baseMint !== 'string') {
    throw new Error('invalid exit intent');
  }
  if (!Array.isArray(parsed.attempts)) parsed.attempts = [];
  return parsed;
}

function safeJson(value: unknown): string {
  return JSON.stringify(value, (_k, v) => (typeof v === 'bigint' ? v.toString() : v));
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function confirmMs(result: BroadcastResult, startedAtMs: number): { exitTriggerToConfirmMs?: number } {
  const value = result.confirmedAtMs ? result.confirmedAtMs - startedAtMs : result.confirmLatencyMs;
  return value !== undefined ? { exitTriggerToConfirmMs: value } : {};
}

function short(mint: string): string {
  return mint.length > 10 ? `${mint.slice(0, 4)}…${mint.slice(-4)}` : mint;
}
