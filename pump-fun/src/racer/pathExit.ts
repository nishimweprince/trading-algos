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
import { paperRoundTripPct, type PoolMark } from './economics.ts';

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
export type ExitRule = 'stop20' | 'take30' | 'trail15' | 'combined' | 'time300';
export const EXIT_RULES: readonly ExitRule[] = ['stop20', 'take30', 'trail15', 'combined', 'time300'];

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
  fixedCostSol: number, rule: ExitRule, maxHoldMs = 300_000): ExitResult {
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
