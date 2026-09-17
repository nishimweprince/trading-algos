import { describe, it, expect } from 'vitest';
import { SlotClock } from '../src/core/slotClock.ts';

describe('SlotClock', () => {
  it('is monotonic and reports age', () => {
    const now = { t: 0 };
    const c = new SlotClock({ now: () => now.t });
    expect(c.get()).toBeUndefined();
    c.observe(100, 'laserstream');
    c.observe(99, 'helius-ws'); // older — ignored
    now.t = 300;
    expect(c.get()).toEqual({ slot: 100, ageMs: 300, source: 'laserstream' });
    expect(c.isLive).toBe(true);
  });

  it('goes stale and falls back to getSlot only then; never throws', async () => {
    const now = { t: 0 };
    let calls = 0;
    const rpc = { getSlot: async () => { calls++; return 500; } };
    const c = new SlotClock({ now: () => now.t, rpc, staleAfterMs: 2_000 });
    c.observe(100, 'ws');
    expect(await c.current()).toBe(100);
    expect(calls).toBe(0);
    now.t = 2_500;
    expect(c.get()).toBeUndefined();
    expect(await c.current()).toBe(500);
    expect(calls).toBe(1);
    const failing = new SlotClock({ rpc: { getSlot: async () => { throw new Error('down'); } } });
    expect(await failing.current()).toBeUndefined();
    expect(await new SlotClock().current()).toBeUndefined();
  });
});
