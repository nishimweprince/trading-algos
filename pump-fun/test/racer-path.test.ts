import { describe, expect, it } from 'vitest';
import { PoolPathRecorder, simulatePathExit, type CapturedPath, type PathState } from '../src/racer/pathExit.ts';

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

  it('prices a rug as a loss and leaves a missing next fill unpriced', () => {
    expect(simulatePathExit(entry, path({ atMs: 1_000, slot: 101, signature: 'rug', mark: 'gone' }), 0, 0.1, 0.001, 'combined'))
      .toEqual({ reason: 'rug', pnlPct: -101, exitAtMs: 1_000 });
    expect(simulatePathExit(entry, path(state(1_000, 0.7)), 0, 0.1, 0, 'stop20').reason).toBe('unpriced-exit');
    expect(simulatePathExit(entry, { quality: 'missing-anchor', states: [state(1_000, 10)] }, 0, 0.1, 0, 'take30').pnlPct).toBeNull();
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
