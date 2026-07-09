import type { Config } from '../config/schema.ts';
import type { TypedBus } from '../core/bus.ts';
import type { ExitTrigger, Mint, PoolPricingRef, Position } from '../core/types.ts';
import type { BroadcastResult } from '../executor/broadcaster.ts';
import type { Executor } from '../executor/index.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { ExitLadder } from './presign.ts';
import type { Fill } from './position.ts';
import { logger } from '../core/logger.ts';

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
}

export interface ExitOutcome {
  confirmed: boolean;
  intent: LiveExitIntent;
  result?: BroadcastResult | undefined;
  remainingRawAmount: bigint;
  exitTx?: string | undefined;
  exitTriggerToConfirmMs?: number | undefined;
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

    while (intent.attempts.length < this.config.exits.maxExitAttempts) {
      const waitMs = Math.max(0, intent.nextRetryAtMs - this.now());
      if (waitMs > 0) await delay(waitMs);

      const slippagePct = this.slippageForAttempt(intent);
      try {
        const result = await this.broadcastAttempt(args, intent, slippagePct);
        lastResult = result;
        exitTx = result.signature;
        intent.attempts.push({
          route: result.route,
          signature: result.signature,
          bundleId: result.bundleId,
          confirmed: result.confirmed,
          slippagePct,
          atMs: this.now(),
          ...(result.sendErr ? { error: String(result.sendErr) } : {}),
        });
        remaining = result.confirmed
          ? await this.executor.reconcileTokenBalance(args.pricing.baseMint, args.pricing.baseIsToken2022 ?? false)
          : BigInt(intent.remainingRawAmount);
        intent.remainingRawAmount = remaining.toString();

        if (result.confirmed && (!intent.fullRemainder || remaining <= 0n)) {
          intent.status = 'confirmed';
          this.persistIntent(args.position, args, intent, result);
          return {
            confirmed: true,
            intent,
            result,
            remainingRawAmount: remaining,
            ...(exitTx ? { exitTx } : {}),
            ...confirmMs(result, startedAtMs),
          };
        }

        intent.status = 'retrying';
        intent.lastError = result.confirmed
          ? `confirmed but token balance remains ${remaining.toString()}`
          : `unconfirmed sell: ${String(result.sendErr ?? 'unknown')}`;
      } catch (err) {
        intent.status = 'retrying';
        intent.lastError = (err as Error).message;
        intent.attempts.push({ confirmed: false, slippagePct, atMs: this.now(), error: intent.lastError });
      }

      intent.nextRetryAtMs = this.now() + this.config.exits.exitRetryMs;
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

  private async broadcastAttempt(args: StartExitArgs, intent: LiveExitIntent, slippagePct: number): Promise<BroadcastResult> {
    const raw = BigInt(intent.remainingRawAmount) < BigInt(intent.targetRawAmount)
      ? BigInt(intent.remainingRawAmount)
      : BigInt(intent.targetRawAmount);
    if (raw <= 0n) throw new Error('nothing to sell');
    if (intent.fullRemainder && args.ladder && !args.ladder.isStale(this.config.exits.ladderRefreshMs)) {
      const tier = intent.trigger === 'EMERGENCY_EXIT' || intent.trigger === 'KILL_SWITCH'
        ? args.ladder.worst()
        : args.ladder.pick(slippagePct);
      if (tier) return this.executor.broadcastSignedExit(tier.bytes, intent.baseMint);
    }
    return this.executor.sellAndConfirm(intent.poolAddress, intent.baseMint, raw, slippagePct);
  }

  private slippageForAttempt(intent: LiveExitIntent): number {
    if (!intent.fullRemainder) return this.config.entry.maxSlippagePct;
    if (intent.trigger === 'EMERGENCY_EXIT' || intent.trigger === 'KILL_SWITCH') return this.worstSlippage();
    const tiers = [...this.config.exits.ladderSlippageTiers].sort((a, b) => a - b);
    return tiers[Math.min(intent.attempts.length, tiers.length - 1)] ?? this.worstSlippage();
  }

  private worstSlippage(): number {
    const tiers = this.config.exits.ladderSlippageTiers;
    return tiers[tiers.length - 1] ?? this.config.entry.maxSlippagePct;
  }

  private rawTarget(args: StartExitArgs): bigint {
    if (args.fullRemainder) return args.rawBaseAmount;
    const scaled = BigInt(Math.floor(args.fill.fraction * 1_000_000));
    const target = (args.originalRawBaseAmount * scaled) / 1_000_000n;
    return target < args.rawBaseAmount ? target : args.rawBaseAmount;
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
