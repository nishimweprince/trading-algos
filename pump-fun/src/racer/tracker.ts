/**
 * Event-driven racer tracker (pure state machine; replaces the RPC poller).
 *
 * Fed by the racer stream, in chain order per curve:
 *
 *   onCurve    — a bonding-curve write inside the stream band. Admits the
 *                mint at or under `admitCostSol`, fires once at or under
 *                `maxCostSol`, and stamps the first `complete` write.
 *   onMigrate  — a landed migrate tx for the mint: emits `migrated` with the
 *                admit / fire / complete / migrate slots (all chain slots from
 *                the updates themselves), whether we were tracking it or not.
 *   sweep      — `complete-no-pool` when a completed curve saw no migrate in
 *                `completeNoPoolMs`; `expired` past the track window.
 *
 * Every timestamp that matters is a chain slot carried by the update, so
 * fire→complete and complete→migrate are measurements of the race, not of
 * our own polling.
 */
import type { CurveReserves } from '../enrichment/curve.ts';
import { completionCostSol } from './economics.ts';

export type RacerOutcome = 'migrated' | 'complete-no-pool' | 'expired';

export interface CurveState {
  mint: string;
  curve: string;
  admitSlot: number | null;
  admitCostSol: number | null;
  fireSlot: number | null;
  fireCostSol: number | null;
  completeSlot: number | null;
  completeAtMs: number | null;
  firstSeenMs: number;
}

export type TrackerEvent =
  | { kind: 'admitted'; state: CurveState }
  | { kind: 'would-fire'; state: CurveState }
  | { kind: 'complete'; state: CurveState }
  | { kind: 'closed'; outcome: Exclude<RacerOutcome, 'migrated'>; state: CurveState }
  | { kind: 'migrated'; state: CurveState | null; mint: string; migrateSlot: number | null };

export interface TrackerOptions {
  admitCostSol: number;
  maxCostSol: number;
  feeBps: number;
  trackWindowMs: number;
  completeNoPoolMs: number;
  maxTracked: number;
}

export class RacerTracker {
  private readonly opts: TrackerOptions;
  private readonly sink: (e: TrackerEvent) => void;
  private readonly now: () => number;
  private readonly states = new Map<string, CurveState>();
  /** Mints already migrated: late curve writes for them are ignored. */
  private readonly done = new Set<string>();

  constructor(opts: TrackerOptions, sink: (e: TrackerEvent) => void, now: () => number = Date.now) {
    this.opts = opts;
    this.sink = sink;
    this.now = now;
  }

  get size(): number {
    return this.states.size;
  }

  get(mint: string): CurveState | undefined {
    return this.states.get(mint);
  }

  onCurve(mint: string, curve: string, r: CurveReserves, slot: number | null): void {
    if (this.done.has(mint)) return;
    let st = this.states.get(mint);
    if (r.complete) {
      // A drained complete curve has already migrated (post-migration writes
      // keep arriving); only one still holding its SOL is about to.
      if (r.realSolReserves === 0n) return;
      if (!st) st = this.admit(mint, curve, slot, null);
      if (st.completeSlot === null) {
        st.completeSlot = slot;
        st.completeAtMs = this.now();
        this.sink({ kind: 'complete', state: { ...st } });
      }
      return;
    }
    const cost = completionCostSol(r, this.opts.feeBps);
    if (cost === null) return;
    if (!st) {
      if (cost > this.opts.admitCostSol) return;
      st = this.admit(mint, curve, slot, cost);
    }
    if (st.fireSlot === null && cost <= this.opts.maxCostSol) {
      st.fireSlot = slot;
      st.fireCostSol = cost;
      this.sink({ kind: 'would-fire', state: { ...st } });
    }
  }

  onMigrate(mint: string, migrateSlot: number | null): void {
    const st = this.states.get(mint) ?? null;
    this.states.delete(mint);
    this.done.add(mint);
    if (this.done.size > 20_000) this.done.clear();
    this.sink({ kind: 'migrated', state: st ? { ...st } : null, mint, migrateSlot });
  }

  sweep(): void {
    const now = this.now();
    for (const [mint, st] of this.states) {
      if (st.completeAtMs !== null) {
        if (now - st.completeAtMs >= this.opts.completeNoPoolMs) {
          this.states.delete(mint);
          this.sink({ kind: 'closed', outcome: 'complete-no-pool', state: { ...st } });
        }
        continue;
      }
      if (now - st.firstSeenMs >= this.opts.trackWindowMs) {
        this.states.delete(mint);
        this.sink({ kind: 'closed', outcome: 'expired', state: { ...st } });
      }
    }
  }

  private admit(mint: string, curve: string, slot: number | null, cost: number | null): CurveState {
    if (this.states.size >= this.opts.maxTracked) {
      // Drop the oldest open (non-complete) curve; it closes as expired.
      for (const [m, s] of this.states) {
        if (s.completeAtMs !== null) continue;
        this.states.delete(m);
        this.sink({ kind: 'closed', outcome: 'expired', state: { ...s } });
        break;
      }
    }
    const st: CurveState = {
      mint,
      curve,
      admitSlot: slot,
      admitCostSol: cost,
      fireSlot: null,
      fireCostSol: null,
      completeSlot: null,
      completeAtMs: null,
      firstSeenMs: this.now(),
    };
    this.states.set(mint, st);
    this.sink({ kind: 'admitted', state: { ...st } });
    return st;
  }
}
