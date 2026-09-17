import { describe, it, expect } from 'vitest';
import { FeedCoverage } from '../src/detector/coverage.ts';

describe('FeedCoverage', () => {
  it('settles a mint after settleMs with every feed that reported it', () => {
    const now = { t: 1000 };
    const c = new FeedCoverage({ settleMs: 20_000, now: () => now.t });
    c.observe('A', 'pumpportal');
    now.t += 5_000;
    c.observe('A', 'laserstream');
    c.observe('B', 'laserstream');
    now.t += 15_000; // A is 20 s old, B 15 s
    const settled = c.settle();
    expect(settled).toHaveLength(1);
    expect(settled[0]!.mint).toBe('A');
    expect([...settled[0]!.feeds].sort()).toEqual(['laserstream', 'pumpportal']);
    expect(c.size).toBe(1);
    now.t += 5_000;
    expect(c.settle().map((s) => s.mint)).toEqual(['B']);
    expect(c.size).toBe(0);
  });
});
