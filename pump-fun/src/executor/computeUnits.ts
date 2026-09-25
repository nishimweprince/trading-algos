/**
 * Compute-unit limit from measured simulations (work plan 2026-09-25 P4.1).
 *
 * Every swap tx asks for a fixed 250,000 CU; a tight limit makes the priority
 * fee (micro-lamports x CU LIMIT) cheaper and the tx more attractive to a
 * leader. The limit is the max of the last `window` simulated `unitsConsumed`
 * for that tx kind x (1 + marginPct), within [floor, cap] — and the default
 * until `minSamples` exist, so a cold start can never under-provision.
 */
export class ComputeUnitTracker {
  private readonly samples = new Map<string, number[]>();
  private readonly opts: { window?: number; minSamples?: number; marginPct?: number; floor?: number; cap?: number; fallback?: number };

  constructor(opts: ComputeUnitTracker['opts'] = {}) {
    this.opts = opts;
  }

  record(kind: string, unitsConsumed: number | undefined | null): void {
    if (!unitsConsumed || !Number.isFinite(unitsConsumed) || unitsConsumed <= 0) return;
    const arr = this.samples.get(kind) ?? [];
    arr.push(unitsConsumed);
    if (arr.length > (this.opts.window ?? 20)) arr.shift();
    this.samples.set(kind, arr);
  }

  limitFor(kind: string): number {
    const fallback = this.opts.fallback ?? 250_000;
    const arr = this.samples.get(kind) ?? [];
    if (arr.length < (this.opts.minSamples ?? 5)) return fallback;
    const want = Math.ceil(Math.max(...arr) * (1 + (this.opts.marginPct ?? 15) / 100));
    return Math.min(this.opts.cap ?? 400_000, Math.max(this.opts.floor ?? 60_000, want));
  }
}
