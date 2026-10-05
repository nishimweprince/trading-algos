/**
 * Realistic (slot-delayed) first-position entries.
 *
 * The paper `reach` entry buys at the pool right after the migrate tx — ahead
 * of every later buyer. Only the migrator can be there. A reactive bot sees
 * the migrate and lands in a later position: at best behind every other tx
 * in the migrate slot. Live 2026-10-05: several pools took 170–500 SOL of
 * buys within seconds, so that gap is the whole question.
 *
 * Entry `+k` (`slotK`) = the pool at the END of slot (migrateSlot + k − 1):
 * landing FIRST in slot S+k, behind everything that settled before it (+1 =
 * behind the migrate slot's same-slot buyers). That is optimistic within the
 * slot, so `slotKmid` lands at the MEDIAN position among slot S+k's own
 * writes — a reactive bot competing with every other one landing there.
 *
 * Pricing needs only the base vault: PumpSwap is constant-product on
 * (vault quote + virtual quote) × base, so with k = E_reach · B_reach (E in
 * SOL) the mark at base B is E = k / B and price = E / (B / 10^dec). Verified
 * on live pools: (Q + V) · B stayed at 1.76e10 while the virtual quote
 * itself moved. LP fees grow k slightly, so the price is a hair low.
 *
 * The base vault is the canonical pool's ATA for the mint, so it is armed at
 * curve admission. A delayed entry is priced only when the migrate tx's vault
 * write anchors the slot; late watches and missing anchors are unverified.
 */
import type { PoolMark } from './economics.ts';
import type { TrackerEvent } from './tracker.ts';

/** Migrated watches remain armed until the worker finishes pricing their entries. */
export function handleTrackerVaultWatch(
  event: TrackerEvent,
  arm: (mint: string) => void,
  release: (mint: string) => void,
): void {
  if (event.kind === 'admitted') arm(event.state.mint);
  else if (event.kind === 'closed') release(event.state.mint);
}

/** Token account amount: u64 LE at offset 64 (SPL and Token-2022 base layout). */
export function decodeTokenAmount(bytes: Uint8Array): bigint | null {
  if (bytes.length < 72) return null;
  return Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).readBigUInt64LE(64);
}

export interface VaultUpdate {
  slot: number;
  amount: bigint;
  /** The tx that wrote it (base58), when the stream carries it. */
  signature?: string | null;
}

export type EntryQuality = 'verified' | 'late-armed' | 'missing-anchor';

/** A missing migrate-slot anchor cannot be replaced by the post-tx reach mark. */
function hasAnchor(updates: readonly VaultUpdate[], migrateSlot: number, reachBase: bigint): boolean {
  return updates.some((u) => u.slot === migrateSlot && u.amount === reachBase);
}

/**
 * Entry marks at +k slots for each k in `ks`, from base-vault updates (arrival
 * order), anchored on an observed migrate-slot vault write and the reach mark.
 */
export function slotEntries(
  updates: readonly VaultUpdate[],
  migrateSlot: number,
  reach: PoolMark,
  reachBase: bigint,
  ks: readonly number[],
  decimals = 6,
): Record<string, PoolMark | null> {
  const out: Record<string, PoolMark | null> = {};
  if (!(reach.quoteSol > 0) || reachBase <= 0n || !hasAnchor(updates, migrateSlot, reachBase)) {
    for (const k of ks) {
      out[`slot${k}`] = null;
      out[`slot${k}mid`] = null;
    }
    return out;
  }
  const kInv = reach.quoteSol * Number(reachBase);
  const mark = (b: bigint): PoolMark => {
    const quoteSol = kInv / Number(b);
    return { price: quoteSol / (Number(b) / 10 ** decimals), quoteSol };
  };
  for (const k of ks) {
    const cutoff = migrateSlot + k - 1;
    let base: bigint | null = null;
    for (const u of updates) if (u.slot >= migrateSlot && u.slot <= cutoff && u.amount > 0n) base = u.amount;
    const first = base ?? reachBase;
    out[`slot${k}`] = mark(first);
    // Median landing position among slot S+k's own writes (position 0 = first).
    const inSlot = updates.filter((u) => u.slot === migrateSlot + k && u.amount > 0n).map((u) => u.amount);
    const states = [first, ...inSlot];
    out[`slot${k}mid`] = mark(states[Math.floor((states.length - 1) / 2)]!);
  }
  return out;
}

/** Signatures of the buys (base vault decreasing) that landed in `slot`, in order. */
export function buyerSignatures(updates: readonly VaultUpdate[], slot: number): string[] {
  const out: string[] = [];
  let prev: bigint | null = null;
  for (const u of updates) {
    if (u.slot === slot && prev !== null && u.amount < prev && u.signature) out.push(u.signature);
    if (u.slot <= slot) prev = u.amount;
  }
  return [...new Set(out)];
}

interface Armed {
  vaults: string[];
  armedAtMs: number;
  /** True when armed before the migrate (normally at admission). */
  early: boolean;
  updates: VaultUpdate[];
}

/** Holds base-vault watches per mint and their updates until the entries are priced. */
export class SlotEntryTracker {
  private readonly byMint = new Map<string, Armed>();
  private readonly byVault = new Map<string, string>();
  private readonly now: () => number;
  private readonly ttlMs: number;

  constructor(opts: { ttlMs?: number; now?: () => number } = {}) {
    this.ttlMs = opts.ttlMs ?? 120_000;
    this.now = opts.now ?? Date.now;
  }

  /** Watch these candidate base-vault addresses for `mint`. Returns true when the watch set changed. */
  arm(mint: string, vaults: string[], early: boolean): boolean {
    if (this.byMint.has(mint)) return false;
    this.byMint.set(mint, { vaults, armedAtMs: this.now(), early, updates: [] });
    for (const v of vaults) this.byVault.set(v, mint);
    return true;
  }

  isWatched(address: string): boolean {
    return this.byVault.has(address);
  }

  watched(): string[] {
    return [...this.byVault.keys()];
  }

  onVault(address: string, amount: bigint, slot: number | null, signature: string | null = null): void {
    const mint = this.byVault.get(address);
    if (!mint || slot === null) return;
    this.byMint.get(mint)?.updates.push({ slot, amount, signature });
  }

  /** Only writes received by this instant count as a decision-time observation. */
  observedSameSlotDepletionPct(mint: string, slot: number | null, migrateSignature: string, reachBase: bigint): number | null {
    const updates = this.byMint.get(mint)?.updates;
    if (!updates || slot === null || reachBase <= 0n) return null;
    const anchor = updates.findIndex((u) => u.slot === slot && u.signature === migrateSignature && u.amount === reachBase);
    if (anchor < 0) return null;
    const last = updates.slice(anchor).filter((u) => u.slot === slot).at(-1);
    return last ? Math.max(0, (1 - Number(last.amount) / Number(reachBase)) * 100) : null;
  }

  /** Price and release a mint's watch. Null when it was never armed. */
  take(
    mint: string,
    migrateSlot: number,
    migrateSignature: string,
    reach: PoolMark,
    reachBase: bigint,
    ks: readonly number[],
  ): { entries: Record<string, PoolMark | null>; early: boolean; quality: EntryQuality; sameSlotDepletionPct: number | null; updates: number; slot1Buyers: string[] } | null {
    const a = this.byMint.get(mint);
    if (!a) return null;
    this.release(mint);
    const anchorIndex = a.updates.findIndex((u) => u.slot === migrateSlot && u.signature === migrateSignature && u.amount === reachBase);
    const quality: EntryQuality = !a.early ? 'late-armed' : anchorIndex < 0 ? 'missing-anchor' : 'verified';
    const afterAnchor = anchorIndex < 0 ? [] : a.updates.slice(anchorIndex).filter((u) => u.slot === migrateSlot && u.amount > 0n);
    const lastBase = afterAnchor.at(-1)?.amount;
    const sameSlotDepletionPct = lastBase === undefined || reachBase <= 0n ? null : Math.max(0, (1 - Number(lastBase) / Number(reachBase)) * 100);
    return {
      entries: anchorIndex < 0 ? Object.fromEntries(ks.flatMap((k) => [[`slot${k}`, null], [`slot${k}mid`, null]])) : slotEntries(a.updates.slice(anchorIndex), migrateSlot, reach, reachBase, ks),
      early: a.early,
      quality,
      sameSlotDepletionPct,
      updates: a.updates.length,
      slot1Buyers: anchorIndex < 0 ? [] : buyerSignatures(a.updates.slice(anchorIndex), migrateSlot + 1),
    };
  }

  release(mint: string): boolean {
    const a = this.byMint.get(mint);
    if (!a) return false;
    this.byMint.delete(mint);
    for (const v of a.vaults) this.byVault.delete(v);
    return true;
  }

  /** Drop watches that never saw a migrate. Returns true when the watch set changed. */
  sweep(): boolean {
    const cutoff = this.now() - this.ttlMs;
    let changed = false;
    for (const [mint, a] of this.byMint) if (a.armedAtMs < cutoff) changed = this.release(mint) || changed;
    return changed;
  }
}
