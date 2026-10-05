import { describe, expect, it } from 'vitest';
import { PoolPathRecorder, horizonExitFromPath, simulatePathExit, type CapturedPath, type PathState } from '../src/racer/pathExit.ts';

const entry = { price: 1, quoteSol: 100 };
const state = (atMs: number, price: number, realQuoteSol = 100): PathState => ({
  atMs, slot: 100 + Math.floor(atMs / 400), signature: `s${atMs}`,
  mark: { price, quoteSol: 100, realQuoteSol },
});
const path = (...states: PathState[]): CapturedPath => ({ quality: 'verified', states });

describe('racer path exits', () => {
  it('requires both migration vault anchors and pairs later writes by transaction', () => {
    let now = 0;
    const recorder = new PoolPathRecorder(() => now);
    recorder.arm('M', ['B'], 'Q');
    recorder.onVault('B', 1_000_000n, 100, 'm');
    now = 1;
    recorder.onVault('Q', 1_000_000_000n, 100, 'm');
    now = 400;
    recorder.onVault('B', 900_000n, 101, 'buy');
    now = 401;
    recorder.onVault('Q', 1_100_000_000n, 101, 'buy');
    const captured = recorder.finish('M', 100, 'm', 1_000_000n, 1_000_000_000n, 0n);
    expect(captured.quality).toBe('verified');
    expect(captured.states).toHaveLength(1);
    expect(captured.states[0]!.mark).toMatchObject({ realQuoteSol: 1.1 });
    expect(recorder.watched()).toEqual([]);

    recorder.arm('N', ['B2'], 'Q2');
    recorder.onVault('B2', 1_000_000n, 100, 'm');
    expect(recorder.finish('N', 100, 'm', 1_000_000n, 1_000_000_000n, 0n).quality).toBe('missing-anchor');
  });

  it('rejects an unpaired reserve write instead of splicing stale vault balances', () => {
    const recorder = new PoolPathRecorder();
    recorder.arm('M', ['B'], 'Q');
    recorder.onVault('B', 1_000_000n, 100, 'm');
    recorder.onVault('Q', 1_000_000_000n, 100, 'm');
    recorder.onVault('B', 900_000n, 101, 'buy-without-quote');
    expect(recorder.finish('M', 100, 'm', 1_000_000n, 1_000_000_000n, 0n).quality).toBe('incomplete-state');
  });

  it('recognizes a one-sided closed vault as a terminal rug observation', () => {
    const recorder = new PoolPathRecorder();
    recorder.arm('M', ['B'], 'Q');
    recorder.onVault('B', 1_000_000n, 100, 'm');
    recorder.onVault('Q', 1_000_000_000n, 100, 'm');
    recorder.onVault('Q', null, 101, 'close');
    expect(recorder.finish('M', 100, 'm', 1_000_000n, 1_000_000_000n, 0n).states[0]!.mark).toBe('gone');
  });

  it('fills a gapped stop at the next observed state, not at the trigger', () => {
    const result = simulatePathExit(entry, path(state(1_000, 0.7), state(2_000, 0.4)), 0, 0.1, 0, 'stop20');
    expect(result.reason).toBe('stop');
    expect(result.exitAtMs).toBe(2_000);
    expect(result.pnlPct!).toBeLessThan(-50);
  });

  it('prices a rug as a loss; a missing next fill is unpriced only across a stream gap', () => {
    expect(simulatePathExit(entry, path({ atMs: 1_000, slot: 101, signature: 'rug', mark: 'gone' }), 0, 0.1, 0.001, 'combined'))
      .toEqual({ reason: 'rug', pnlPct: -101, exitAtMs: 1_000 });
    const gapped: CapturedPath = { quality: 'verified', states: [state(1_000, 0.7)], gapsMs: [1_500] };
    expect(simulatePathExit(entry, gapped, 0, 0.1, 0, 'stop20').reason).toBe('unpriced-exit');
    expect(simulatePathExit(entry, { quality: 'missing-anchor', states: [state(1_000, 10)] }, 0, 0.1, 0, 'take30').pnlPct).toBeNull();
  });

  it('a crash followed by no trades fills on the unchanged crashed state (never dropped)', () => {
    // Stop triggers at 0.3 and nothing trades after: the sell lands on that same state.
    const crashed = simulatePathExit(entry, path(state(1_000, 0.9), state(2_000, 0.3)), 0, 0.1, 0, 'stop20');
    expect(crashed).toMatchObject({ reason: 'stop', exitAtMs: 2_000, fill: 'unchanged' });
    expect(crashed.pnlPct!).toBeLessThan(-65);
    // The same trigger with a later trade still fills at the next state.
    expect(simulatePathExit(entry, path(state(2_000, 0.3), state(3_000, 0.2)), 0, 0.1, 0, 'stop20')).toMatchObject({ exitAtMs: 3_000 });
    expect(simulatePathExit(entry, path(state(2_000, 0.3), state(3_000, 0.2)), 0, 0.1, 0, 'stop20').fill).toBeUndefined();
  });

  it('a dead pool exits at the deadline on its last state, or on the entry state if nothing traded', () => {
    const quiet = simulatePathExit(entry, path(state(10_000, 0.95)), 0, 0.1, 0.001, 'time300');
    expect(quiet).toMatchObject({ reason: 'time', exitAtMs: 300_000, fill: 'unchanged' });
    expect(quiet.pnlPct!).toBeLessThan(-5);
    const untouched = simulatePathExit(entry, path(), 1_000, 0.1, 0.001, 'combined');
    expect(untouched).toMatchObject({ reason: 'time', exitAtMs: 301_000, fill: 'unchanged' });
    // Round trip at an unchanged price: only fees, impact and the fixed cost.
    expect(untouched.pnlPct!).toBeLessThan(0);
    expect(untouched.pnlPct!).toBeGreaterThan(-5);
    // A stream gap during the hold makes the same quiet pool unknowable.
    expect(simulatePathExit(entry, { quality: 'verified', states: [], gapsMs: [50_000] }, 1_000, 0.1, 0, 'combined').reason).toBe('unpriced-exit');
  });

  it('records stream gaps per watched pool; gaps before entry do not count', () => {
    let now = 0;
    const recorder = new PoolPathRecorder(() => now);
    recorder.arm('M', ['B'], 'Q');
    recorder.onVault('B', 1_000_000n, 100, 'm');
    recorder.onVault('Q', 1_000_000_000n, 100, 'm');
    now = 200;
    recorder.markGap();
    const captured = recorder.finish('M', 100, 'm', 1_000_000n, 1_000_000_000n, 0n);
    expect(captured.gapsMs).toEqual([200]);
    expect(simulatePathExit(entry, captured, 1_000, 0.1, 0, 'combined')).toMatchObject({ reason: 'time', fill: 'unchanged' });
    expect(simulatePathExit(entry, captured, 100, 0.1, 0, 'combined').reason).toBe('unpriced-exit');
    // A pool armed after the gap is unaffected.
    recorder.arm('N', ['B2'], 'Q2');
    recorder.onVault('B2', 1_000_000n, 100, 'm2');
    recorder.onVault('Q2', 1_000_000_000n, 100, 'm2');
    expect(recorder.finish('N', 100, 'm2', 1_000_000n, 1_000_000_000n, 0n).gapsMs).toEqual([]);
  });

  it('applies a quote-vault cap and stop-before-time priority', () => {
    const result = simulatePathExit(entry, path(state(300_000, 0.7), state(301_000, 0.5, 0.01)), 0, 0.1, 0, 'combined');
    expect(result.reason).toBe('stop');
    expect(result.pnlPct!).toBeLessThan(-89);
  });

  it('uses the following state for trailing and time exits', () => {
    const trailing = simulatePathExit(entry, path(state(1_000, 1.5), state(2_000, 1.2), state(3_000, 0.8)), 0, 0.1, 0, 'trail15');
    expect(trailing).toMatchObject({ reason: 'trail', exitAtMs: 3_000 });
    const timed = simulatePathExit(entry, path(state(299_000, 1), state(300_000, 1), state(301_000, 0.9)), 0, 0.1, 0, 'time300');
    expect(timed).toMatchObject({ reason: 'time', exitAtMs: 300_000 });
  });
});

describe('horizon exits from stream states', () => {
  const anchor = { mark: { price: 1, quoteSol: 100 }, slot: 100 };
  const at = (atMs: number, slot: number, price: number): PathState => ({ atMs, slot, signature: `t${atMs}`, mark: { price, quoteSol: 100 } });

  it('decides on the last state by the horizon and fills on the first state in a later slot', () => {
    const p = path(at(1_000, 101, 1.2), at(30_000, 160, 1.5), at(30_100, 160, 1.6), at(30_500, 161, 1.4));
    const h = horizonExitFromPath(p, anchor, 0, 30_200)!;
    expect(h.mid).toMatchObject({ price: 1.6 });
    expect(h.exit).toMatchObject({ price: 1.4 }); // same-slot 30_100 is not a fill; slot 161 is
    expect(h.fill).toBe('next-slot');
  });

  it('a quiet pool fills on the unchanged state; nothing traded at all → the migrate anchor', () => {
    expect(horizonExitFromPath(path(at(1_000, 101, 0.8)), anchor, 0, 30_000)).toEqual({ mid: { price: 0.8, quoteSol: 100 }, exit: { price: 0.8, quoteSol: 100 }, fill: 'unchanged' });
    expect(horizonExitFromPath(path(), anchor, 0, 30_000)).toEqual({ mid: anchor.mark, exit: anchor.mark, fill: 'unchanged' });
  });

  it('a rug is a rug; a gap or an unverified path falls back (null)', () => {
    expect(horizonExitFromPath(path({ atMs: 1_000, slot: 101, signature: 'r', mark: 'gone' }), anchor, 0, 30_000)).toMatchObject({ mid: 'gone' });
    expect(horizonExitFromPath({ quality: 'verified', states: [], gapsMs: [5_000] }, anchor, 0, 30_000)).toBeNull();
    expect(horizonExitFromPath({ quality: 'missing-anchor', states: [] }, anchor, 0, 30_000)).toBeNull();
  });
});
