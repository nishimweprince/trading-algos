import { describe, expect, it } from 'vitest';
import {
  measureBuyCostParts,
  routeTipLamports,
  SWQOS_MIN_TIP_LAMPORTS,
  txExecutionCostParts,
  txExecutionCostSol,
  type RawTx,
} from '../src/racer/costs.ts';
import { adjustLegForFill } from '../src/racer/fills.ts';
import { recostPnlForDelta } from '../src/racer/paper.ts';
import { openDb } from '../src/persistence/db.ts';
import { getRacerSummary } from '../src/dashboard/queries.ts';

function mockTx(fee: number, deltas: Array<{ i: number; d: number }>, tokenIdx: number[] = [], err = false): RawTx {
  const pre = [1_000_000_000, 0, 0, 0];
  const post = [...pre];
  for (const { i, d } of deltas) post[i]! += d;
  return {
    meta: {
      err: err ? { InstructionError: [0, 'Custom'] } : null,
      fee,
      preBalances: pre,
      postBalances: post,
      preTokenBalances: [],
      postTokenBalances: tokenIdx.map((accountIndex) => ({ accountIndex })),
    },
  };
}

describe('txExecutionCostParts', () => {
  it('splits the network fee from tips to non-token accounts', () => {
    const parts = txExecutionCostParts(mockTx(5_000, [{ i: 1, d: 100_000 }]));
    expect(parts).toEqual({ feeLamports: 5_000, tipLamports: 100_000 });
  });

  it('excludes token-account balance increases (pool/protocol fees in WSOL)', () => {
    const parts = txExecutionCostParts(mockTx(5_000, [{ i: 1, d: 100_000 }, { i: 2, d: 50_000 }], [2]));
    expect(parts).toEqual({ feeLamports: 5_000, tipLamports: 100_000 });
  });

  it('ignores the payer account and balance decreases', () => {
    const parts = txExecutionCostParts(mockTx(5_000, [{ i: 0, d: 7_000_000 }, { i: 1, d: -3_000 }]));
    expect(parts).toEqual({ feeLamports: 5_000, tipLamports: 0 });
  });

  it('returns null for failed or unreadable transactions', () => {
    expect(txExecutionCostParts(mockTx(5_000, [], [], true))).toBeNull();
    expect(txExecutionCostParts(null)).toBeNull();
    expect(txExecutionCostParts({ meta: { fee: 1 } })).toBeNull();
  });

  it('txExecutionCostSol is the parts sum, so the split cannot drift from the total', () => {
    const tx = mockTx(8_000, [{ i: 1, d: 250_000 }]);
    const parts = txExecutionCostParts(tx)!;
    expect(txExecutionCostSol(tx)).toBe((parts.feeLamports + parts.tipLamports) / 1e9);
  });
});

describe('measureBuyCostParts', () => {
  const txs = [
    mockTx(5_000, [{ i: 1, d: 0 }]),
    mockTx(8_000, [{ i: 1, d: 100_000 }]),
    mockTx(11_000, [{ i: 1, d: 200_000 }]),
  ];

  it('medians the fee and tip sides separately over the sample', async () => {
    const s = await measureBuyCostParts(async (sig) => txs[Number(sig)] ?? null, ['0', '1', '2'], 3);
    expect(s).toEqual({ buyFeeSol: 8_000 / 1e9, buyTipSol: 100_000 / 1e9, n: 3 });
  });

  it('respects the sample cap', async () => {
    const s = await measureBuyCostParts(async (sig) => txs[Number(sig)] ?? null, ['0', '1', '2'], 1);
    expect(s?.n).toBe(1);
    expect(s?.buyTipSol).toBe(0);
  });

  it('retries a fully-missed pass, then gives up with null', async () => {
    let calls = 0;
    const s = await measureBuyCostParts(
      async () => (++calls <= 3 ? null : txs[1]!),
      ['a', 'b', 'c'],
      3,
      { attempts: 3, delayMs: 1, sleep: async () => {} },
    );
    expect(s?.buyFeeSol).toBe(8_000 / 1e9);
    const missed = await measureBuyCostParts(async () => null, ['a'], 1, { sleep: async () => {} });
    expect(missed).toBeNull();
  });

  it('treats a throwing fetch as a miss, and an empty sample as null', async () => {
    const s = await measureBuyCostParts(
      async (sig) => { if (sig === 'bad') throw new Error('rpc'); return txs[0]!; },
      ['bad', '0'],
      2,
      { sleep: async () => {} },
    );
    expect(s?.n).toBe(1);
    expect(await measureBuyCostParts(async () => txs[0]!, [], 3)).toBeNull();
  });
});

describe('routeTipLamports', () => {
  it('prices our route per side, with Max-mode buys as a minTip lower bound', () => {
    const off = { enabled: false, swqosOnly: true, exitSwqosOnly: true, minTipLamports: 5_000 };
    expect(routeTipLamports(off, 'buy')).toBe(0);
    expect(routeTipLamports(off, 'sell')).toBe(0);
    const swqos = { ...off, enabled: true };
    expect(routeTipLamports(swqos, 'buy')).toBe(5_000);
    expect(routeTipLamports(swqos, 'sell')).toBe(5_000);
    const maxExitsCheap = { enabled: true, swqosOnly: false, exitSwqosOnly: true, minTipLamports: 1_000_000 };
    expect(routeTipLamports(maxExitsCheap, 'buy')).toBe(1_000_000);
    expect(routeTipLamports(maxExitsCheap, 'sell')).toBe(SWQOS_MIN_TIP_LAMPORTS);
    const maxBoth = { ...maxExitsCheap, exitSwqosOnly: false };
    expect(routeTipLamports(maxBoth, 'sell')).toBe(1_000_000);
  });
});

describe('adjustLegForFill exit failures', () => {
  it('charges expected exit-send failures on top of entry attempts', () => {
    const v = adjustLegForFill(100, { p: 0.5, costSol: 0.001, exitFailCostSol: 0.003 }, 0.1);
    expect(v).toBeCloseTo(50 - (0.004 / 0.1) * 100, 12);
  });

  it('is unchanged when the field is absent (tracks priced before Unit 5)', () => {
    expect(adjustLegForFill(100, { p: 0.5, costSol: 0.001 }, 0.1)).toBeCloseTo(50 - (0.001 / 0.1) * 100, 12);
  });
});

describe('recostPnlForDelta', () => {
  it('shifts conditional legs and scales the expectancy legs by fill-p', () => {
    const pnl: Record<string, number | null> = { slot1: 100, slot1_adj: 40, slot2: null };
    recostPnlForDelta(pnl, { slot1: { p: 0.5 }, slot2: { p: 0.5 } }, ['slot1', 'slot2'], 2);
    expect(pnl.slot1).toBe(98);
    expect(pnl.slot1_adj).toBe(39);
    expect(pnl.slot2).toBeNull();
  });

  it('only touches the named legs and tolerates missing fill', () => {
    const pnl: Record<string, number | null> = { open: 100, slot1: 100, slot1_adj: 40 };
    recostPnlForDelta(pnl, {}, ['slot1'], 2);
    expect(pnl.open).toBe(100);
    expect(pnl.slot1).toBe(98);
    expect(pnl.slot1_adj).toBe(40);
  });

  it('matches a fresh expectancy computation on the re-costed leg', () => {
    const fill = { p: 0.5, costSol: 0.001, exitFailCostSol: 0.003 };
    const base = 100;
    const pnl: Record<string, number | null> = { slot1: base, slot1_adj: adjustLegForFill(base, fill, 0.1) };
    recostPnlForDelta(pnl, { slot1: fill }, ['slot1'], 2);
    expect(pnl.slot1_adj).toBeCloseTo(adjustLegForFill(pnl.slot1!, fill, 0.1)!, 9);
  });
});

describe('racer summary cost decomposition', () => {
  function seed(): ReturnType<typeof openDb> {
    const db = openDb({ path: ':memory:', memory: true });
    const horizons = JSON.stringify([{ sec: 60, pnl: { slot1mid: 5 }, exitPrice: 1, exitQuoteSol: 10 }]);
    const entries = JSON.stringify({ slot1mid: { price: 1, quoteSol: 10 } });
    const ins = db.prepare(`INSERT INTO racer_paper_tracks
      (mint, size_sol, reach_price, reach_quote_sol, horizons_json, entries_json, created_at, armed,
       entry_model_version, entry_quality, live_cost_sol, live_cost_source, live_cost_buy_fee_sol, live_cost_buy_tip_sol)
      VALUES (?, 0.1, 1, 10, ?, ?, ?, 1, 2, 'verified', 0.001, 'measured', ?, ?)`);
    ins.run('A', horizons, entries, '2026-01-01 00:00:00', 0.000008, 0.0001);
    ins.run('B', horizons, entries, '2026-01-01 00:01:05', 0.00001, 0.0002);
    return db;
  }

  it('surfaces the measured fee/tip medians and our route-tip scenario', () => {
    const db = seed();
    const s = getRacerSummary(db, { routeBuyTipSol: 0.001, routeSellTipSol: 0.000005 }).paper;
    expect(s.measuredCostShare).toBe(1);
    expect(s.medianBuyFeeSol).toBeCloseTo(0.000009, 12);
    expect(s.medianBuyTipSol).toBeCloseTo(0.00015, 12);
    expect(s.routeTips?.buyTipSol).toBe(0.001);
    expect(s.routeTips?.sellTipSol).toBe(0.000005);
    const bare = getRacerSummary(db, {}).paper;
    expect(bare.routeTips).toBeNull();
    expect(bare.medianBuyFeeSol).toBeCloseTo(0.000009, 12);
  });

  it('holds rent locked until the sweeper frees it (never, when sweeping is off)', () => {
    const db = seed();
    const stat = (sweepDelayMs: number) =>
      getRacerSummary(db, { ataRentSol: 0.002, sweepDelayMs }).paper.horizons.find((h) => h.sec === 60)!.sizeScenario;
    // Sweeper off: A's rent is still locked when B enters, so the peak carries it.
    expect(stat(0).peakConcurrentSol).toBeCloseTo(0.104, 9);
    // Fast sweeper: A's rent releases before B enters.
    expect(stat(5_000).peakConcurrentSol).toBeCloseTo(0.102, 9);
  });
});
