/** Ordered, bounded pool-vault path and predeclared paper exit rules. No sends. */
import { LAMPORTS_PER_SOL } from '../core/constants.ts';
import { computePrice } from '../positions/pricing.ts';
import { paperRoundTripPct, type PoolMark } from './economics.ts';

type Side = 'base' | 'quote';
interface Write { side: Side; amount: bigint | null; slot: number; signature: string | null; atMs: number }
interface Watch { addresses: string[]; writes: Write[]; overflow: boolean }
export interface PathState { atMs: number; slot: number; signature: string; mark: PoolMark | 'gone' }
export interface CapturedPath { quality: 'verified' | 'missing-anchor' | 'overflow' | 'incomplete-state'; states: PathState[] }
export interface ExitResult { reason: 'stop' | 'take' | 'trail' | 'time' | 'rug' | 'unpriced-exit'; pnlPct: number | null; exitAtMs: number | null }
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
    this.watches.set(mint, { addresses, writes: [], overflow: false });
    for (const address of bases) this.byAddress.set(address, { mint, side: 'base' });
    this.byAddress.set(quote, { mint, side: 'quote' });
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

  finish(mint: string, migrateSlot: number, migrateSignature: string, reachBase: bigint, reachQuote: bigint, virtualQuote: bigint): CapturedPath {
    const watch = this.watches.get(mint);
    this.release(mint);
    if (!watch) return { quality: 'missing-anchor', states: [] };
    if (watch.overflow) return { quality: 'overflow', states: [] };
    const baseAnchor = watch.writes.findIndex((w) => w.side === 'base' && w.slot === migrateSlot && w.signature === migrateSignature && w.amount === reachBase);
    const quoteAnchor = watch.writes.findIndex((w) => w.side === 'quote' && w.slot === migrateSlot && w.signature === migrateSignature && w.amount === reachQuote);
    if (baseAnchor < 0 || quoteAnchor < 0) return { quality: 'missing-anchor', states: [] };
    // Account updates for two vaults need not arrive adjacent or in the same
    // order. Filter by tx identity, not by the later anchor's array index.
    const after = watch.writes.filter((w) => w.slot >= migrateSlot && w.signature !== migrateSignature);
    if (after.some((w) => w.signature === null)) return { quality: 'incomplete-state', states: [] };
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
        return { quality: 'incomplete-state', states: [] }; // Never combine reserves from different transactions.
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
    return { quality: 'verified', states };
  }

  release(mint: string): void {
    const watch = this.watches.get(mint);
    if (!watch) return;
    this.watches.delete(mint);
    for (const address of watch.addresses) this.byAddress.delete(address);
  }
}

/** Trigger on one complete observed state; fill at the next state, never at the trigger mark. */
export function simulatePathExit(entry: PoolMark, path: CapturedPath, entryAtMs: number, sizeSol: number,
  fixedCostSol: number, rule: ExitRule, maxHoldMs = 300_000): ExitResult {
  const unpriced: ExitResult = { reason: 'unpriced-exit', pnlPct: null, exitAtMs: null };
  if (path.quality !== 'verified' || !(sizeSol > 0)) return unpriced;
  const states = path.states.filter((s) => s.atMs >= entryAtMs);
  const costPct = fixedCostSol / sizeSol * 100;
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
    const next = states[i + 1];
    if (!next) return unpriced;
    if (next.mark === 'gone') return { reason: 'rug', pnlPct: -100 - costPct, exitAtMs: next.atMs };
    const fill = paperRoundTripPct(entry, next.mark, sizeSol);
    return fill === null ? unpriced : { reason: stop ? 'stop' : take ? 'take' : trail ? 'trail' : 'time',
      pnlPct: fill - costPct, exitAtMs: next.atMs };
  }
  return unpriced;
}
