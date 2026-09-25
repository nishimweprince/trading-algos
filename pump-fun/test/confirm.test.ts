import { describe, it, expect } from 'vitest';
import { ConfirmObserver, evaluateConfirm, type ConfirmObservation } from '../src/guardrails/confirmGate.ts';
import { ShadowTracker } from '../src/guardrails/shadow.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import type { PriceRead } from '../src/positions/pricing.ts';

const CRIT = { minNetInflowSol: 0, minPriceUpPct: 0, maxPriceUpPct: 25, maxSingleSellPoolPct: 8, minUniqueBuyers: 0 };
const sol = (n: number) => BigInt(Math.round(n * 1e9));

const obs = (over: Partial<ConfirmObservation> = {}): ConfirmObservation => ({
  delayMs: 15_000, samples: 10, startQuoteSol: 80, endQuoteSol: 84, netInflowSol: 4, priceUpPct: 10,
  maxSingleDropPct: 2, endPrice: 1.1, endBaseReserve: 1n, endQuoteReserveLamports: sol(84), ...over,
});

describe('evaluateConfirm (P3.2)', () => {
  it('passes a second-wave pool that is rising but not blown out', () => {
    expect(evaluateConfirm(obs(), CRIT)).toEqual({ ok: true });
  });
  it.each([
    [{ maxSingleDropPct: 9 }, 'large_sell'],
    [{ netInflowSol: -1 }, 'weak_inflow'],
    [{ priceUpPct: -3 }, 'price_below_migration'],
    [{ priceUpPct: 40 }, 'blown_out'],
    [{ samples: 0 }, 'no_data'],
  ] as const)('rejects %o as %s', (over, reason) => {
    expect(evaluateConfirm(obs(over), CRIT)).toMatchObject({ ok: false, reason });
  });
  it('enforces unique buyers only when configured', () => {
    expect(evaluateConfirm(obs({ uniqueBuyers: 2 }), { ...CRIT, minUniqueBuyers: 5 })).toMatchObject({ ok: false, reason: 'few_buyers' });
  });
});

describe('ConfirmObserver', () => {
  it('serves several delays from one poll loop, tracking inflow and the largest single drop', async () => {
    let t = 0;
    const quotes = [80, 82, 74, 83, 86, 88, 90];
    let i = 0;
    const read = async (): Promise<PriceRead> => {
      const q = quotes[Math.min(i++, quotes.length - 1)]!;
      return { price: q / 80, baseReserve: 1_000n, quoteReserveLamports: sol(q) };
    };
    const got: ConfirmObservation[] = [];
    const observer = new ConfirmObserver({ read, pollMs: 1_000, sleep: async (ms) => { t += ms; }, now: () => t });
    await observer.observe({ baseVault: 'b', quoteVault: 'q', baseDecimals: 6 }, { price: 1, quoteReserveLamports: sol(80) }, [3_000, 6_000], (o) => { got.push(o); });
    expect(got.map((o) => o.delayMs)).toEqual([3_000, 6_000]);
    expect(got[0]!.netInflowSol).toBeCloseTo(-6, 9); // 80 -> 74 at 3 s
    expect(got[0]!.maxSingleDropPct).toBeCloseTo((82 - 74) / 82 * 100, 9);
    expect(got[1]!.netInflowSol).toBeCloseTo(8, 9); // 80 -> 88 at 6 s
    expect(got[1]!.priceUpPct).toBeCloseTo(10, 9);
  });
});

describe('ShadowTracker multi-arm (P3.2)', () => {
  const stubRpc = { getMultipleAccountsBase64: async () => [] } as unknown as RpcClient;
  const ref = { mint: 'M', baseVault: 'b', quoteVault: 'q', baseDecimals: 6 };

  it('runs a veto track and confirm arms for one mint, writing arms to confirm_outcomes with paths', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    let now = 0;
    const tracker = new ShadowTracker(stubRpc, repos, { now: () => now, sizeSol: 0.04, recordPaths: true });
    expect(tracker.track({ mint: 'M', verdict: 'veto', primaryVetoCode: 'H12', baselinePrice: 1, poolRef: ref })).toBe(true);
    expect(tracker.track({ mint: 'M', verdict: 'confirm_arm', arm: 'confirm_5000', primaryVetoCode: null, baselinePrice: 1.1, poolRef: ref })).toBe(true);
    expect(tracker.track({ mint: 'M', verdict: 'confirm_arm', arm: 'confirm_5000', primaryVetoCode: null, baselinePrice: 1.1, poolRef: ref })).toBe(false);
    expect(tracker.size).toBe(2);

    now = 1_000;
    tracker.injectTick('M', 0.5, now); // both stop out
    expect(tracker.size).toBe(0);
    const shadow = db.prepare(`SELECT arm, exit_reason FROM shadow_outcomes`).all();
    const arms = db.prepare(`SELECT arm, verdict, baseline_price FROM confirm_outcomes`).all();
    expect(shadow).toEqual([{ arm: 'veto', exit_reason: 'STOP_LOSS' }]);
    expect(arms).toEqual([{ arm: 'confirm_5000', verdict: 'confirm_arm', baseline_price: 1.1 }]);
    expect(repos.pathTicks('M', 'confirm_5000')).toEqual([{ tMs: 1_000, price: 0.5, quoteReserveSol: null }]);
    tracker.stop();
  });
});
