/**
 * Ordered, bounded pool-vault path and predeclared paper exit rules. No sends.
 *
 * Every swap writes both pool vaults, and the vaults are watched through the
 * longest hold, so NO write means NO trade: the pool still sits at its last
 * observed state. A sell sent then lands on that state. Only a stream gap
 * (reconnect, SDK stream error, failed watch update) makes the absence of
 * writes unknowable, and only then is a missing fill left unpriced — dropping
 * quiet or crashed pools otherwise biases results upward.
 */
import { LAMPORTS_PER_SOL } from '../core/constants.ts';
import { computePrice } from '../positions/pricing.ts';
import { exitImpactPct, paperRoundTripPct, type PoolMark } from './economics.ts';
import { buyFillPrice, sellProceedsSol } from '../positions/ammImpact.ts';
import { feeBpsForMcap, mcapFromPrice } from '../positions/feeTiers.ts';
import { evaluateExit, gainPct, type ExitCfg, type ExitState } from '../exits/engine.ts';

type Side = 'base' | 'quote';
interface Write { side: Side; amount: bigint | null; slot: number; signature: string | null; atMs: number }
interface Watch { addresses: string[]; writes: Write[]; overflow: boolean; gapsMs: number[] }
export interface PathState { atMs: number; slot: number; signature: string; mark: PoolMark | 'gone' }
export interface CapturedPath {
  quality: 'verified' | 'missing-anchor' | 'overflow' | 'incomplete-state';
  states: PathState[];
  /** Stream-gap instants while this pool was watched; absent/empty = continuous. */
  gapsMs?: number[];
}
export interface ExitResult {
  reason: 'stop' | 'take' | 'trail' | 'time' | 'rug' | 'unpriced-exit';
  pnlPct: number | null;
  exitAtMs: number | null;
  /** 'unchanged' = no later trade was observed on a continuous stream; the fill is the last pool state. */
  fill?: 'unchanged';
}
export type ExitRule = 'stop20' | 'take30' | 'trail15' | 'combined' | 'time300' | 'live-fsm';
export const EXIT_RULES: readonly ExitRule[] = ['stop20', 'take30', 'trail15', 'combined', 'time300', 'live-fsm'];

export class PoolPathRecorder {
  private readonly watches = new Map<string, Watch>();
  private readonly byAddress = new Map<string, { mint: string; side: Side }>();
  private readonly now: () => number;
  private readonly maxWrites: number;
  constructor(now: () => number = Date.now, maxWrites = 10_000) {
    this.now = now;
    this.maxWrites = maxWrites;
  }

  arm(mint: string, bases: string[], quote: string): void {
    if (this.watches.has(mint)) return;
    const addresses = [...new Set([...bases, quote])];
    this.watches.set(mint, { addresses, writes: [], overflow: false, gapsMs: [] });
    for (const address of bases) this.byAddress.set(address, { mint, side: 'base' });
    this.byAddress.set(quote, { mint, side: 'quote' });
  }

  /** The stream may have dropped writes now: every pool watched at this instant has a gap. */
  markGap(atMs: number = this.now()): void {
    for (const watch of this.watches.values()) watch.gapsMs.push(atMs);
  }

  isWatched(address: string): boolean { return this.byAddress.has(address); }
  watched(): string[] { return [...this.byAddress.keys()]; }

  onVault(address: string, amount: bigint | null, slot: number | null, signature: string | null): void {
    const ref = this.byAddress.get(address);
    if (!ref || slot === null) return;
    const watch = this.watches.get(ref.mint)!;
    if (watch.writes.length >= this.maxWrites) { watch.overflow = true; return; }
    watch.writes.push({ side: ref.side, amount, slot, signature, atMs: this.now() });
  }

  midSlotEntryAt(mint: string, slot: number, fallbackMs: number): number {
    const writes = this.watches.get(mint)?.writes.filter((w) => w.side === 'base' && w.slot === slot && w.amount !== null) ?? [];
    return writes.length ? writes[Math.floor((writes.length - 1) / 2)]!.atMs : fallbackMs;
  }

  /** The final path; releases the watch. */
  finish(mint: string, migrateSlot: number, migrateSignature: string, reachBase: bigint, reachQuote: bigint, virtualQuote: bigint): CapturedPath {
    const path = this.snapshot(mint, migrateSlot, migrateSignature, reachBase, reachQuote, virtualQuote);
    this.release(mint);
    return path;
  }

  /** The path observed so far, without releasing the watch (horizon exits read it live). */
  snapshot(mint: string, migrateSlot: number, migrateSignature: string, reachBase: bigint, reachQuote: bigint, virtualQuote: bigint): CapturedPath {
    const watch = this.watches.get(mint);
    if (!watch) return { quality: 'missing-anchor', states: [] };
    if (watch.overflow) return { quality: 'overflow', states: [], gapsMs: watch.gapsMs };
    const baseAnchor = watch.writes.findIndex((w) => w.side === 'base' && w.slot === migrateSlot && w.signature === migrateSignature && w.amount === reachBase);
    const quoteAnchor = watch.writes.findIndex((w) => w.side === 'quote' && w.slot === migrateSlot && w.signature === migrateSignature && w.amount === reachQuote);
    if (baseAnchor < 0 || quoteAnchor < 0) return { quality: 'missing-anchor', states: [], gapsMs: watch.gapsMs };
    // Account updates for two vaults need not arrive adjacent or in the same
    // order. Filter by tx identity, not by the later anchor's array index.
    const after = watch.writes.filter((w) => w.slot >= migrateSlot && w.signature !== migrateSignature);
    if (after.some((w) => w.signature === null)) return { quality: 'incomplete-state', states: [], gapsMs: watch.gapsMs };
    const byTx = new Map<string, Write[]>();
    for (const w of after) {
      if (!w.signature) continue;
      const group = byTx.get(w.signature) ?? [];
      group.push(w);
      byTx.set(w.signature, group);
    }
    const states: PathState[] = [];
    for (const [signature, writes] of byTx) {
      const base = writes.filter((w) => w.side === 'base').at(-1);
      const quote = writes.filter((w) => w.side === 'quote').at(-1);
      if ((!base || !quote) && !writes.some((w) => w.amount === null || w.amount === 0n))
        return { quality: 'incomplete-state', states: [], gapsMs: watch.gapsMs }; // Never combine reserves from different transactions.
      const terminal = writes.find((w) => w.amount === null || w.amount === 0n);
      if (terminal && (!base || !quote)) {
        states.push({ atMs: terminal.atMs, slot: terminal.slot, signature, mark: 'gone' });
        continue;
      }
      if (!base || !quote) continue;
      const atMs = Math.max(base.atMs, quote.atMs);
      const slot = Math.max(base.slot, quote.slot);
      if (base.amount === null || quote.amount === null || base.amount === 0n || quote.amount === 0n) {
        states.push({ atMs, slot, signature, mark: 'gone' });
        continue;
      }
      const price = computePrice(base.amount, quote.amount, 6, virtualQuote);
      if (!(price > 0) || !Number.isFinite(price)) continue;
      states.push({ atMs, slot, signature, mark: { price,
        quoteSol: Number(quote.amount + virtualQuote) / LAMPORTS_PER_SOL,
        realQuoteSol: Number(quote.amount) / LAMPORTS_PER_SOL } });
    }
    states.sort((a, b) => a.atMs - b.atMs || a.slot - b.slot || a.signature.localeCompare(b.signature));
    return { quality: 'verified', states, gapsMs: watch.gapsMs };
  }

  release(mint: string): void {
    const watch = this.watches.get(mint);
    if (!watch) return;
    this.watches.delete(mint);
    for (const address of watch.addresses) this.byAddress.delete(address);
  }
}

/**
 * Trigger on one complete observed state; fill at the next state, never at
 * the trigger mark. When no later state exists on a continuous stream, the
 * pool did not trade again, so the sell fills on the unchanged last state
 * (and a hold with no trigger exits at the deadline on the last state, or on
 * the entry state when nothing traded after entry). With a stream gap inside
 * the hold those fills are unknowable and the exit stays unpriced.
 */
export function simulatePathExit(entry: PoolMark, path: CapturedPath, entryAtMs: number, sizeSol: number,
  fixedCostSol: number, rule: Exclude<ExitRule, 'live-fsm'>, maxHoldMs = 300_000): ExitResult {
  const unpriced: ExitResult = { reason: 'unpriced-exit', pnlPct: null, exitAtMs: null };
  if (path.quality !== 'verified' || !(sizeSol > 0)) return unpriced;
  const states = path.states.filter((s) => s.atMs >= entryAtMs);
  const costPct = fixedCostSol / sizeSol * 100;
  const continuous = !(path.gapsMs ?? []).some((g) => g >= entryAtMs);
  let peak = -Infinity;
  for (let i = 0; i < states.length; i++) {
    const state = states[i]!;
    if (state.mark === 'gone') return { reason: 'rug', pnlPct: -100 - costPct, exitAtMs: state.atMs };
    const gross = paperRoundTripPct(entry, state.mark, sizeSol);
    if (gross === null) return unpriced;
    const net = gross - costPct;
    peak = Math.max(peak, net);
    const stop = (rule === 'stop20' || rule === 'combined') && net <= -20;
    const take = (rule === 'take30' || rule === 'combined') && net >= 30;
    const trail = (rule === 'trail15' || rule === 'combined') && peak > 0 && peak - net >= 15;
    const time = state.atMs >= entryAtMs + maxHoldMs;
    if (!stop && !take && !trail && !time) continue;
    if (time && !stop && !take && !trail) return { reason: 'time', pnlPct: net, exitAtMs: state.atMs };
    const reason = stop ? 'stop' : take ? 'take' : trail ? 'trail' : 'time';
    const next = states[i + 1];
    if (!next) return continuous ? { reason, pnlPct: net, exitAtMs: state.atMs, fill: 'unchanged' } : unpriced;
    if (next.mark === 'gone') return { reason: 'rug', pnlPct: -100 - costPct, exitAtMs: next.atMs };
    const fill = paperRoundTripPct(entry, next.mark, sizeSol);
    return fill === null ? unpriced : { reason, pnlPct: fill - costPct, exitAtMs: next.atMs };
  }
  // No trigger and no state at or past the deadline: the pool sat still.
  if (!continuous) return unpriced;
  const last = states.at(-1);
  const lastMark = last && last.mark !== 'gone' ? last.mark : entry;
  const gross = paperRoundTripPct(entry, lastMark, sizeSol);
  return gross === null ? unpriced : { reason: 'time', pnlPct: gross - costPct, exitAtMs: entryAtMs + maxHoldMs, fill: 'unchanged' };
}

const FSM_EPSILON = 1e-9;

/** Deterministic [0, 1) draw: FNV-1a over `${seed}:${n}`. Exit-failure draws must replay identically. */
export function fsmDraw(seed: string, n: number): number {
  let h = 0x811c9dc5;
  const s = `${seed}:${n}`;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0) / 0xffffffff;
}

export interface FsmCosts {
  /** Buy-side execution (fees + tips), charged once. */
  buyCostSol: number;
  /** Sell-side execution per exit leg. */
  sellCostSol: number;
  /** Network fee per failed exit send. */
  failFeeSol: number;
}

export interface FsmExitLeg {
  trigger: string;
  reason: string;
  /** Fraction of the ORIGINAL position sold. */
  fraction: number;
  proceedsSol: number;
  /** sellCostSol + failed-send fees for this leg. */
  costSol: number;
  exitAtMs: number;
  fill: 'next-slot' | 'unchanged';
  /** Filled past the worst ordinary tier, inside the emergency bound. */
  emergency: boolean;
  attempts: number;
}

export interface FsmExitResult extends ExitResult {
  legs: FsmExitLeg[];
}

export interface FsmExitOpts {
  maxHoldMs?: number | undefined;
  /** Draw seed (the mint): identical inputs replay identical failures. */
  seed?: string | undefined;
  highVolatility?: boolean | undefined;
  /** Per-send land probability (trailing ok-rate); default 1 = sends land. */
  exitP?: number | undefined;
  /** Worst ordinary ladder tier; past it a leg needs the emergency bound. */
  maxExitSlippagePct?: number | undefined;
  emergencySlippagePct?: number | undefined;
  maxAttempts?: number | undefined;
}

function mapFsmReason(trigger: string): ExitResult['reason'] {
  if (trigger === 'STOP_LOSS') return 'stop';
  if (trigger === 'TRAILING_STOP') return 'trail';
  if (trigger === 'TIME_STOP') return 'time';
  if (trigger === 'RUG') return 'rug';
  return 'take';
}

/**
 * The live exit strategy on a captured pool path: the REAL evaluateExit over
 * every observed state, with PaperPosition-identical state transitions (stop
 * ratchet, TP flags, trailing arm). Entry fills at the buy impact price (the
 * honest cost basis); each exit leg fills at the next state (never the
 * trigger mark), gated by the ladder tiers with emergency escalation, with
 * seeded send-failure retries that cost fees and slip fills. A fill across a
 * stream gap is unknowable → the whole sim is unpriced, like the toy rules.
 *
 * Conditional on the entry filling (entry fill-p lives in the `_adj` horizon
 * legs). Static barriers: no P3.5 volatility retune. Partial fills follow the
 * ladder fractions; buy costs once, sells cost per leg.
 */
export function simulateFsmExit(
  entry: PoolMark,
  path: CapturedPath,
  entryAtMs: number,
  sizeSol: number,
  costs: FsmCosts,
  cfg: ExitCfg,
  opts: FsmExitOpts = {},
): FsmExitResult {
  const unpriced: FsmExitResult = { reason: 'unpriced-exit', pnlPct: null, exitAtMs: null, legs: [] };
  if (path.quality !== 'verified' || !(sizeSol > 0)) return unpriced;
  const maxHoldMs = opts.maxHoldMs ?? 300_000;
  const exitP = opts.exitP ?? 1;
  const maxAttempts = opts.maxAttempts ?? 3;
  const maxExit = opts.maxExitSlippagePct ?? 25;
  const emergencyBound = opts.emergencySlippagePct ?? 90;
  const states = path.states.filter((s) => s.atMs >= entryAtMs);
  const gaps = (path.gapsMs ?? []).filter((g) => g >= entryAtMs);
  const continuous = gaps.length === 0;
  const crossed = (fromMs: number, toMs: number) => gaps.some((g) => g > fromMs && g <= toMs);
  // Honest cost basis: what the buy paid per token at size.
  const buyFee = feeBpsForMcap(mcapFromPrice(entry.price)) / 10_000;
  const netIn = sizeSol * (1 - buyFee);
  const entryFill = buyFillPrice(entry.price, netIn, entry.quoteSol);
  if (!(entryFill > 0) || !Number.isFinite(entryFill)) return unpriced;
  const totalTokens = netIn / entryFill;
  const st: ExitState = {
    entryPrice: entryFill,
    openedAtMs: entryAtMs,
    highWaterPrice: entryFill,
    tp0Done: false,
    tp1Done: false,
    trailingArmed: false,
    highVolatility: opts.highVolatility ?? false,
    stopPrice: entryFill * (1 - cfg.hardStopPct / 100),
  };
  let remaining = 1;
  const legs: FsmExitLeg[] = [];
  let simFailFees = 0;
  let drawCount = 0;
  const seed = opts.seed ?? '';
  const draw = (): boolean => exitP >= 1 || fsmDraw(seed, drawCount++) < exitP;

  const makeLeg = (
    legTokens: number, fraction: number, mark: PoolMark, atMs: number, fill: 'next-slot' | 'unchanged',
    emergency: boolean, attempts: number, failFees: number, trigger: string, reason: string,
  ): FsmExitLeg => {
    const sellFee = feeBpsForMcap(mcapFromPrice(mark.price)) / 10_000;
    let proceeds = sellProceedsSol(legTokens * mark.price, mark.quoteSol) * (1 - sellFee);
    if (mark.realQuoteSol !== undefined) proceeds = Math.min(proceeds, Math.max(0, mark.realQuoteSol));
    return { trigger, reason, fraction, proceedsSol: proceeds, costSol: costs.sellCostSol + failFees,
      exitAtMs: atMs, fill, emergency, attempts };
  };

  /** Fill `sellNow` (fraction of original) from trigger state `i`. Null = carry (re-trigger later); throws when unknowable/rug. */
  const fillLeg = (i: number, sellNow: number, trigger: string, reason: string): FsmExitLeg | null => {
    const trig = states[i]!;
    if (trig.mark === 'gone') return null;
    const legTokens = sellNow * totalTokens;
    let failFees = 0;
    // No later state: the pool sat still — fill unchanged, but only when the
    // stream proves it. Sends can still fail on a still pool: retries fill
    // the same state, each failure paid.
    if (i + 1 >= states.length) {
      if (!continuous) return null;
      for (let a = 0; a < maxAttempts; a++) {
        if (!draw()) {
          failFees += costs.failFeeSol;
          continue;
        }
        return makeLeg(legTokens, sellNow, trig.mark, trig.atMs, 'unchanged', false, a + 1, failFees, trigger, reason);
      }
      simFailFees += failFees;
      return null;
    }
    for (let a = 0; a < maxAttempts; a++) {
      const cand = states[i + 1 + a];
      if (!cand) break; // ran past the last state: carry, the trigger re-fires later
      if (crossed(trig.atMs, cand.atMs)) throw UNPRICED;
      if (cand.mark === 'gone') throw rugAt(cand.atMs);
      const impact = exitImpactPct(legTokens * cand.mark.price, cand.mark);
      if (impact > emergencyBound) continue; // live simulates first: an unfillable send is never sent, never paid
      if (!draw()) {
        failFees += costs.failFeeSol;
        continue;
      }
      return makeLeg(legTokens, sellNow, cand.mark, cand.atMs, 'next-slot', impact > maxExit, a + 1, failFees, trigger, reason);
    }
    simFailFees += failFees;
    return null;
  };

  const applyDecision = (trigger: string, fraction: number): void => {
    if (trigger === 'TAKE_PROFIT_0') {
      st.tp0Done = true;
      st.stopPrice = Math.max(st.stopPrice, st.entryPrice * (1 + cfg.tp0MoveStopToPct / 100));
    }
    if (trigger === 'TAKE_PROFIT_1') {
      st.tp1Done = true;
      st.stopPrice = st.entryPrice * (1 + cfg.tp1MoveStopToPct / 100);
    }
    remaining = Math.max(0, remaining - fraction);
  };

  const finalize = (reason: ExitResult['reason'], exitAtMs: number | null): FsmExitResult => {
    let out = 0;
    for (const leg of legs) out += leg.proceedsSol - leg.costSol;
    out -= simFailFees;
    const pnlPct = ((out - costs.buyCostSol - sizeSol) / sizeSol) * 100;
    if (!Number.isFinite(pnlPct)) return { ...unpriced };
    const fill = legs.length > 0 && legs.every((l) => l.fill === 'unchanged') ? 'unchanged' as const : undefined;
    return { reason, pnlPct, exitAtMs, ...(fill ? { fill } : {}), legs };
  };

  try {
    const deadline = entryAtMs + maxHoldMs;
    for (let i = 0; i < states.length && remaining > FSM_EPSILON; i++) {
      const state = states[i]!;
      if (state.mark === 'gone') {
        // Remainder is a total loss; filled legs keep their proceeds.
        return finalize('rug', state.atMs);
      }
      const price = state.mark.price;
      if (price > st.highWaterPrice) st.highWaterPrice = price;
      if (!st.trailingArmed && gainPct(st.entryPrice, price) >= cfg.trailingArmPct) st.trailingArmed = true;
      const decision = state.atMs >= deadline
        ? { trigger: 'TIME_STOP' as const, sellFraction: 1, reason: `hold deadline ${maxHoldMs / 1000}s` }
        : evaluateExit(st, price, state.atMs, cfg);
      if (!decision) continue;
      const sellNow = Math.min(decision.sellFraction >= 1 ? remaining : decision.sellFraction, remaining);
      if (sellNow <= FSM_EPSILON) continue;
      const leg = fillLeg(i, sellNow, decision.trigger, decision.reason);
      if (!leg) continue; // exhausted attempts: the trigger re-fires on a later tick
      legs.push(leg);
      applyDecision(decision.trigger, sellNow);
      if (decision.trigger === 'TIME_STOP') break;
    }
    if (remaining > FSM_EPSILON) {
      // Nothing tradeable after the last tick: exit the remainder at the end
      // of the observed hold, through the same fill path (draws included).
      const lastIdx = states.length - 1;
      const leg = lastIdx >= 0
        ? fillLeg(lastIdx, remaining, 'TIME_STOP', 'end of observed hold')
        : null;
      if (!leg) {
        if (states.length === 0 && continuous) {
          // The pool never traded after entry: exit at the entry mark, with
          // the same send-failure draws (retries fill the same mark).
          let failFees = 0;
          let attempts = 0;
          let landed = false;
          for (let a = 0; a < maxAttempts; a++) {
            attempts = a + 1;
            if (draw()) {
              landed = true;
              break;
            }
            failFees += costs.failFeeSol;
          }
          if (!landed) {
            simFailFees += failFees;
            return unpriced;
          }
          const sellFee = feeBpsForMcap(mcapFromPrice(entry.price)) / 10_000;
          const proceeds = sellProceedsSol(remaining * totalTokens * entry.price, entry.quoteSol) * (1 - sellFee);
          legs.push({ trigger: 'TIME_STOP', reason: 'no trades after entry', fraction: remaining,
            proceedsSol: entry.realQuoteSol !== undefined ? Math.min(proceeds, Math.max(0, entry.realQuoteSol)) : proceeds,
            costSol: costs.sellCostSol + failFees, exitAtMs: entryAtMs, fill: 'unchanged', emergency: false, attempts });
          remaining = 0;
        } else {
          return unpriced;
        }
      } else {
        legs.push(leg);
        remaining = 0;
      }
    }
    const lastTrigger = legs.at(-1)?.trigger ?? 'TIME_STOP';
    return finalize(mapFsmReason(lastTrigger), legs.at(-1)?.exitAtMs ?? entryAtMs);
  } catch (e) {
    if (e === UNPRICED) return unpriced;
    if (typeof e === 'object' && e !== null && 'rugAtMs' in e) return finalize('rug', (e as { rugAtMs: number }).rugAtMs);
    throw e;
  }
}

const UNPRICED = Symbol('unpriced');
const rugAt = (rugAtMs: number): { rugAtMs: number } => ({ rugAtMs });

/** A horizon exit priced from stream states (see horizonExitFromPath). */
export interface PathHorizon {
  /** Pool state at the horizon decision (last complete state at or before it). */
  mid: PoolMark | 'gone';
  /** State the sell lands on: the first complete state in a LATER slot, else the unchanged decision state. */
  exit: PoolMark | 'gone' | null;
  fill: 'next-slot' | 'unchanged' | 'unpriced';
}

/**
 * Decide at `horizonAtMs` on the last observed state (or the migrate anchor
 * when nothing traded since), and fill at the first state in a slot after the
 * decision's slot — the sell lands in the next slot. With no later state on a
 * continuous stream the pool is unchanged and the sell fills there. Null when
 * the path is not verified or a stream gap may have hidden trades (callers fall
 * back to an RPC read).
 */
export function horizonExitFromPath(
  path: CapturedPath,
  anchor: { mark: PoolMark; slot: number },
  entryAtMs: number,
  horizonAtMs: number,
): PathHorizon | null {
  if (path.quality !== 'verified') return null;
  if ((path.gapsMs ?? []).some((g) => g >= entryAtMs)) return null;
  let decision: { mark: PoolMark | 'gone'; slot: number } = anchor;
  let i = 0;
  for (; i < path.states.length && path.states[i]!.atMs <= horizonAtMs; i++) decision = path.states[i]!;
  if (decision.mark === 'gone') return { mid: 'gone', exit: 'gone', fill: 'next-slot' };
  const next = path.states.slice(i).find((s) => s.slot > decision.slot);
  if (next) return { mid: decision.mark, exit: next.mark, fill: 'next-slot' };
  return { mid: decision.mark, exit: decision.mark, fill: 'unchanged' };
}
