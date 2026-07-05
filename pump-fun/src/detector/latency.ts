/**
 * Rolling latency statistics for the detection pipeline (Section 4.2 /
 * Phase 1 deliverable: "latency stats"). Keeps a bounded window of samples and
 * reports percentiles on demand.
 */
export class LatencyStats {
  private readonly samples: number[] = [];
  private readonly cap: number;
  private total = 0;

  constructor(cap = 500) {
    this.cap = cap;
  }

  add(ms: number): void {
    this.total++;
    this.samples.push(ms);
    if (this.samples.length > this.cap) this.samples.shift();
  }

  get count(): number {
    return this.total;
  }

  percentile(p: number): number {
    if (this.samples.length === 0) return 0;
    const sorted = [...this.samples].sort((a, b) => a - b);
    const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
    return sorted[idx] ?? 0;
  }

  summary(): { count: number; p50: number; p95: number; max: number } {
    const max = this.samples.length ? Math.max(...this.samples) : 0;
    return { count: this.total, p50: this.percentile(50), p95: this.percentile(95), max };
  }
}
