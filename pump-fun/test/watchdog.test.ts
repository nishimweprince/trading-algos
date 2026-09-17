import { describe, it, expect } from 'vitest';
import { FeedWatchdog } from '../src/detector/watchdog.ts';

function make(now: { t: number }) {
  return new FeedWatchdog({
    slotSilenceMs: 5_000,
    portalSilenceMs: 600_000,
    portalMissedGraduations: 3,
    reconnectMaxMs: 30_000,
    now: () => now.t,
  });
}

describe('FeedWatchdog', () => {
  it('applies the slot bound to slot feeds and the silence bound to the rest', () => {
    const now = { t: 1_000_000 };
    const w = make(now);
    w.register('laserstream', 'slot');
    w.register('pumpportal', 'silence');
    now.t += 4_900;
    expect(w.tick()).toEqual([]);
    now.t += 200;
    const d = w.tick();
    expect(d.map((x) => x.feed)).toEqual(['laserstream']);
    expect(d[0]!.silentMs).toBe(5_100);
    now.t += 594_800; // 599.9 s of portal silence
    expect(w.tick().map((x) => x.feed)).toEqual([]);
    now.t += 200;
    expect(w.tick().map((x) => x.feed)).toEqual(['pumpportal']);
  });

  it('touch() resets the silence clock and liveness is read lazily', () => {
    const now = { t: 0 };
    const w = make(now);
    let mode: 'slot' | 'silence' = 'slot';
    w.register('helius-ws', () => mode);
    now.t = 4_000;
    w.touch('helius-ws', now.t);
    now.t = 8_000;
    expect(w.tick()).toEqual([]);
    now.t = 9_100;
    expect(w.tick()).toHaveLength(1);
    // Degraded to silence: 5 s is no longer enough.
    w.markConnected('helius-ws', now.t);
    mode = 'silence';
    now.t += 10_000;
    expect(w.tick()).toEqual([]);
  });

  it('suppresses a reconnecting feed until markConnected, with a safety re-arm', () => {
    const now = { t: 0 };
    const w = make(now);
    w.register('laserstream', 'slot');
    now.t = 6_000;
    expect(w.tick()).toHaveLength(1);
    now.t = 12_000;
    expect(w.tick()).toEqual([]); // reconnecting — suppressed
    w.markConnected('laserstream', now.t);
    now.t = 18_000;
    expect(w.tick()).toHaveLength(1); // silent again after reconnecting
    // Never reconnects: re-armed after 2 × max(bound, reconnectMaxMs) = 60 s, then decides 5 s later.
    now.t = 18_000 + 60_000;
    expect(w.tick()).toEqual([]);
    now.t += 5_100;
    expect(w.tick()).toHaveLength(1);
  });

  it('portal miss streak fires on the Nth miss and resets on a hit', () => {
    const w = make({ t: 0 });
    expect(w.portalMissed()).toBe(false);
    expect(w.portalMissed()).toBe(false);
    w.portalHit();
    expect(w.portalMissed()).toBe(false);
    expect(w.portalMissed()).toBe(false);
    expect(w.portalMissed()).toBe(true);
    expect(w.portalMissed()).toBe(false); // reset after firing
  });
});
