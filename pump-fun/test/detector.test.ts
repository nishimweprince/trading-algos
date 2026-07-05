import { describe, it, expect } from 'vitest';
import { MintDedupe } from '../src/detector/dedupe.ts';
import { LatencyStats } from '../src/detector/latency.ts';

describe('MintDedupe', () => {
  it('treats first sighting as new and repeats as duplicate', () => {
    let now = 1000;
    const d = new MintDedupe(5000, () => now);
    expect(d.firstSeen('mintA')).toBe(true);
    expect(d.firstSeen('mintA')).toBe(false);
    expect(d.firstSeen('mintB')).toBe(true);
  });

  it('allows a mint again after the TTL window and evicts stale entries', () => {
    let now = 0;
    const d = new MintDedupe(1000, () => now);
    expect(d.firstSeen('m')).toBe(true);
    now = 500;
    expect(d.firstSeen('m')).toBe(false);
    now = 1600; // past TTL from last insert
    expect(d.firstSeen('m')).toBe(true);
    // Stale unrelated entries are evicted on access.
    now = 5000;
    d.firstSeen('other');
    expect(d.size).toBe(1);
  });
});

describe('LatencyStats', () => {
  it('reports percentiles over samples', () => {
    const s = new LatencyStats();
    for (let i = 1; i <= 100; i++) s.add(i);
    const sum = s.summary();
    expect(sum.count).toBe(100);
    expect(sum.max).toBe(100);
    expect(sum.p50).toBeGreaterThanOrEqual(49);
    expect(sum.p50).toBeLessThanOrEqual(51);
    expect(sum.p95).toBeGreaterThanOrEqual(94);
    expect(sum.p95).toBeLessThanOrEqual(96);
  });

  it('is empty-safe', () => {
    const s = new LatencyStats();
    expect(s.summary()).toEqual({ count: 0, p50: 0, p95: 0, max: 0 });
  });
});
