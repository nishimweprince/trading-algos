import { describe, expect, it } from 'vitest';
import { completionCostSol, curveProgress, evaluateFire, paperRoundTripPct } from '../src/racer/economics.ts';
import { RacerTracker, type TrackerEvent } from '../src/racer/tracker.ts';
import { PaperTracker, type PaperTrack } from '../src/racer/paper.ts';
import { MintResolver } from '../src/racer/mintResolver.ts';
import { buildRacerRequest, routeUpdate, REAL_TOKEN_TOP_OFFSET, type CurveUpdate } from '../src/racer/stream.ts';
import { extractMigrateTx, instructionTrace, migrateEntry } from '../src/racer/migrateTx.ts';
import { BONDING_CURVE_DISCRIMINATOR, deriveBondingCurvePda, type CurveReserves } from '../src/enrichment/curve.ts';
import { PROGRAM_IDS, WSOL_MINT } from '../src/core/constants.ts';

function reserves(over: Partial<CurveReserves> = {}): CurveReserves {
  return {
    curveAddress: 'curve',
    // Near-complete shape: most virtual tokens bought, little real left.
    virtualTokenReserves: 200_000_000_000n,
    virtualSolReserves: 150_000_000_000n,
    realTokenReserves: 1_000_000_000n,
    realSolReserves: 80_000_000_000n,
    tokenTotalSupply: 1_000_000_000_000_000n,
    complete: false,
    ...over,
  };
}

/** pump.fun curve at `realTokens` raw left, on the standard launch shape (vT0 1073M, vS0 30 SOL, real0 793.1M). */
function standardCurve(realTokens: bigint, over: Partial<CurveReserves> = {}): CurveReserves {
  const vT0 = 1_073_000_000_000_000n;
  const vS0 = 30_000_000_000n;
  const real0 = 793_100_000_000_000n;
  const vT = vT0 - (real0 - realTokens);
  const vS = (vT0 * vS0) / vT;
  return reserves({ virtualTokenReserves: vT, virtualSolReserves: vS, realTokenReserves: realTokens, realSolReserves: vS - vS0, ...over });
}

function curveBytes(r: CurveReserves): Uint8Array {
  const buf = Buffer.alloc(81);
  Buffer.from(BONDING_CURVE_DISCRIMINATOR, 'hex').copy(buf, 0);
  buf.writeBigUInt64LE(r.virtualTokenReserves, 8);
  buf.writeBigUInt64LE(r.virtualSolReserves, 16);
  buf.writeBigUInt64LE(r.realTokenReserves, 24);
  buf.writeBigUInt64LE(r.realSolReserves, 32);
  buf.writeBigUInt64LE(r.tokenTotalSupply, 40);
  buf[48] = r.complete ? 1 : 0;
  return Uint8Array.from(buf);
}

describe('completionCostSol', () => {
  it('prices a constant-product drain of the remaining real tokens', () => {
    // k = 2e11 * 1.5e11 = 3e22; remaining vT = 2e11 - 1e9 = 1.99e11;
    // cost = 3e22/1.99e11 - 1.5e11 ≈ 7.5377e8 lamports ≈ 0.7538 SOL.
    expect(completionCostSol(reserves())).toBeCloseTo(0.7538, 3);
  });
  it('grosses the cost up by the curve fee', () => {
    expect(completionCostSol(reserves(), 125)).toBeCloseTo(0.7538 / (1 - 0.0125), 3);
  });
  it('returns 0 for complete/empty curves and null for unpriced shapes', () => {
    expect(completionCostSol(reserves({ complete: true }))).toBe(0);
    expect(completionCostSol(reserves({ realTokenReserves: 0n }))).toBe(0);
    expect(completionCostSol(reserves({ virtualTokenReserves: 0n }))).toBeNull();
    expect(completionCostSol(reserves({ virtualTokenReserves: 500n, realTokenReserves: 999n }))).toBeNull();
  });
  it('costs ~85 SOL on a fresh curve — why launch-time admission never admitted (G1)', () => {
    expect(completionCostSol(standardCurve(793_100_000_000_000n))).toBeCloseTo(85, 0);
  });
});

describe('curveProgress / evaluateFire', () => {
  it('tracks graduation progress and gates on the cost ceiling', () => {
    expect(curveProgress(reserves())).toBeCloseTo(80 / 85, 4);
    expect(curveProgress(reserves({ complete: true }))).toBe(1);
    expect(evaluateFire(reserves(), 1).fire).toBe(true);
    expect(evaluateFire(reserves(), 0.1)).toMatchObject({ fire: false, reason: 'too-expensive' });
    expect(evaluateFire(null, 1).reason).toBe('no-curve');
    expect(evaluateFire(reserves({ complete: true }), 1).reason).toBe('complete');
  });
});

describe('paperRoundTripPct', () => {
  it('charges tiered fees both ways and impact on thin pools', () => {
    const deep = { price: 1e-7, quoteSol: 1e9 };
    const flat = paperRoundTripPct(deep, deep, 0.1)!;
    // Flat price on an effectively infinite pool: only the two fee legs.
    expect(flat).toBeLessThan(0);
    expect(flat).toBeGreaterThan(-5);
    const thin = paperRoundTripPct({ price: 1e-7, quoteSol: 1 }, { price: 1e-7, quoteSol: 1 }, 0.1)!;
    expect(thin).toBeLessThan(flat);
    expect(paperRoundTripPct(deep, { price: 2e-7, quoteSol: 1e9 }, 0.1)!).toBeGreaterThan(90);
    expect(paperRoundTripPct({ price: 0, quoteSol: 1 }, deep, 0.1)).toBeNull();
  });
});

describe('stream band filter', () => {
  it('subscribes one memcmp per byte-29 value plus migrate and create txs', () => {
    const req = buildRacerRequest(12) as { accounts: Record<string, { filters: unknown[] }>; transactions: Record<string, { failed?: boolean }> };
    expect(Object.keys(req.accounts)).toHaveLength(12);
    expect(req.accounts.band0!.filters).toHaveLength(2);
    // Failed migrate attempts are wanted; failed creates are not.
    expect(req.transactions.migrate!.failed).toBeUndefined();
    expect(req.transactions.creates!.failed).toBe(false);
  });
  it('band 12 covers ~5 SOL of completion and excludes a fresh curve', () => {
    const edge = standardCurve(12n * 2n ** 40n - 1n);
    const bytes = curveBytes(edge);
    expect(bytes[REAL_TOKEN_TOP_OFFSET + 1]).toBe(0);
    expect(bytes[REAL_TOKEN_TOP_OFFSET + 2]).toBe(0);
    expect(bytes[REAL_TOKEN_TOP_OFFSET]).toBe(11);
    expect(completionCostSol(edge)!).toBeGreaterThan(5);
    expect(completionCostSol(edge)!).toBeLessThan(5.5);
    const fresh = curveBytes(standardCurve(793_100_000_000_000n));
    expect(fresh[REAL_TOKEN_TOP_OFFSET + 1]! + fresh[REAL_TOKEN_TOP_OFFSET + 2]!).toBeGreaterThan(0);
  });
  it('routes a curve account update with its slot and writing signature', () => {
    const got: CurveUpdate[] = [];
    const kind = routeUpdate(
      {
        account: {
          slot: '453529843',
          account: { pubkey: 'CurveKey', owner: PROGRAM_IDS.PUMP_FUN, data: curveBytes(standardCurve(1_000_000_000_000n)), txnSignature: new Uint8Array(64).fill(1) },
        },
      },
      { onCurve: (u) => void got.push(u), onMigrateTx: () => {}, onLaunch: () => {} },
    );
    expect(kind).toBe('account');
    expect(got[0]).toMatchObject({ curve: 'CurveKey', slot: 453529843 });
    expect(got[0]!.signature).toBeTruthy();
    expect(got[0]!.reserves.realTokenReserves).toBe(1_000_000_000_000n);
  });
});

describe('RacerTracker', () => {
  const opts = { admitCostSol: 5, maxCostSol: 0.5, feeBps: 0, trackWindowMs: 60_000, completeNoPoolMs: 10_000, maxTracked: 3 };
  function setup() {
    let now = 0;
    const events: TrackerEvent[] = [];
    const t = new RacerTracker(opts, (e) => void events.push(e), () => now);
    return { t, events, advance: (ms: number) => (now += ms) };
  }
  const tokensForCost = { admit: 4_000_000_000_000n, fire: 500_000_000_000n, out: 100_000_000_000_000n };

  it('ignores curves above the admit band, admits inside it, fires once', () => {
    const { t, events } = setup();
    t.onCurve('M', 'C', standardCurve(tokensForCost.out), 1);
    expect(events).toHaveLength(0);
    t.onCurve('M', 'C', standardCurve(tokensForCost.admit), 2);
    expect(events.map((e) => e.kind)).toEqual(['admitted']);
    t.onCurve('M', 'C', standardCurve(tokensForCost.fire), 5);
    t.onCurve('M', 'C', standardCurve(tokensForCost.admit), 6); // a sell backs it out
    t.onCurve('M', 'C', standardCurve(tokensForCost.fire), 7); // re-entry: no second fire
    expect(events.map((e) => e.kind)).toEqual(['admitted', 'would-fire']);
    expect(t.get('M')).toMatchObject({ admitSlot: 2, fireSlot: 5 });
  });

  it('stamps complete then emits migrated with chain slots', () => {
    const { t, events } = setup();
    t.onCurve('M', 'C', standardCurve(tokensForCost.fire), 10);
    t.onCurve('M', 'C', standardCurve(0n, { complete: true, realSolReserves: 85_000_000_000n }), 12);
    t.onCurve('M', 'C', standardCurve(0n, { complete: true, realSolReserves: 85_000_000_000n }), 13);
    t.onMigrate('M', 15);
    const mig = events.find((e) => e.kind === 'migrated');
    expect(mig).toMatchObject({ kind: 'migrated', mint: 'M', migrateSlot: 15, state: { fireSlot: 10, completeSlot: 12 } });
    expect(events.filter((e) => e.kind === 'complete')).toHaveLength(1);
    expect(t.size).toBe(0);
  });

  it('labels complete-no-pool only after the window, not at the flip (G3)', () => {
    const { t, events, advance } = setup();
    t.onCurve('M', 'C', standardCurve(0n, { complete: true, realSolReserves: 85_000_000_000n }), 12);
    advance(3_000);
    t.sweep();
    expect(events.some((e) => e.kind === 'closed')).toBe(false);
    t.onMigrate('M', 14);
    expect(events.at(-1)).toMatchObject({ kind: 'migrated', state: { completeSlot: 12 } });

    t.onCurve('N', 'C2', standardCurve(0n, { complete: true, realSolReserves: 85_000_000_000n }), 20);
    advance(10_000);
    t.sweep();
    expect(events.at(-1)).toMatchObject({ kind: 'closed', outcome: 'complete-no-pool' });
  });

  it('ignores drained (already migrated) curves and expires stale ones', () => {
    const { t, events, advance } = setup();
    t.onCurve('M', 'C', standardCurve(0n, { complete: true, realSolReserves: 0n }), 1);
    expect(events).toHaveLength(0);
    t.onCurve('N', 'C2', standardCurve(tokensForCost.admit), 1);
    advance(60_000);
    t.sweep();
    expect(events.at(-1)).toMatchObject({ kind: 'closed', outcome: 'expired' });
  });

  it('records untracked migrations with a null state', () => {
    const { t, events } = setup();
    t.onMigrate('X', 99);
    expect(events).toEqual([{ kind: 'migrated', state: null, mint: 'X', migrateSlot: 99 }]);
  });
});

describe('migrate tx parsing', () => {
  const MINT = 'Mint1111111111111111111111111111111111111111';
  const POOL = 'Pool1111111111111111111111111111111111111111';
  const CALLER = 'Caller11111111111111111111111111111111111111';
  const pumpIx = (name: string) => [`Program ${PROGRAM_IDS.PUMP_FUN} invoke [1]`, `Program log: Instruction: ${name}`, `Program ${PROGRAM_IDS.PUMP_FUN} success`];
  const swapIx = (name: string, depth = 1) => [`Program ${PROGRAM_IDS.PUMP_SWAP} invoke [${depth}]`, `Program log: Instruction: ${name}`, `Program ${PROGRAM_IDS.PUMP_SWAP} success`];
  function txUpdate(logs: string[], post: Array<{ mint: string; owner: string; amount: string }>, pre: typeof post = [], err: unknown = null) {
    const bal = (bs: typeof post) => bs.map((b) => ({ mint: b.mint, owner: b.owner, uiTokenAmount: { amount: b.amount } }));
    return {
      transaction: {
        slot: '100',
        transaction: {
          signature: 'Sig1',
          transaction: { message: { accountKeys: [CALLER] } },
          meta: { err, logMessages: logs, preTokenBalances: bal(pre), postTokenBalances: bal(post) },
        },
      },
    };
  }

  it('attributes instructions through the CPI stack', () => {
    const trace = instructionTrace([
      `Program Wrapper invoke [1]`,
      `Program ${PROGRAM_IDS.PUMP_FUN} invoke [2]`,
      'Program log: Instruction: MigrateV2',
      `Program ${PROGRAM_IDS.PUMP_SWAP} invoke [3]`,
      'Program log: Instruction: CreatePool',
      `Program ${PROGRAM_IDS.PUMP_SWAP} success`,
      `Program ${PROGRAM_IDS.PUMP_FUN} success`,
      `Program Wrapper success`,
    ]);
    expect(trace).toEqual([
      { program: PROGRAM_IDS.PUMP_FUN, name: 'MigrateV2' },
      { program: PROGRAM_IDS.PUMP_SWAP, name: 'CreatePool' },
    ]);
  });

  it('a plain migrate: open = reach, priced from the pool vaults', () => {
    const up = txUpdate(pumpIx('MigrateV2'), [
      { mint: MINT, owner: POOL, amount: '206900000000000' },
      { mint: WSOL_MINT, owner: POOL, amount: '84000000000' },
    ]);
    const tx = extractMigrateTx(up)!;
    expect(tx).toMatchObject({ ok: true, caller: CALLER, mint: MINT, slot: 100, curveBuyBefore: false, poolBuyAfter: false });
    const e = migrateEntry(tx, MINT, POOL, 0n)!;
    expect(e.reach.price).toBeCloseTo(84 / 206_900_000, 12);
    expect(e.open).toEqual(e.reach);
  });

  it('a bundled pool buy: open backs the buy out along the curve', () => {
    const B0 = 206_900_000_000_000n;
    const bought = 10_000_000_000_000n;
    const Q0 = 84_000_000_000n;
    const B1 = B0 - bought;
    const Q1 = (B0 * Q0) / B1;
    const up = txUpdate(
      [...pumpIx('MigrateV2'), ...swapIx('Buy')],
      [
        { mint: MINT, owner: POOL, amount: B1.toString() },
        { mint: WSOL_MINT, owner: POOL, amount: Q1.toString() },
        { mint: MINT, owner: CALLER, amount: bought.toString() },
      ],
    );
    const tx = extractMigrateTx(up)!;
    expect(tx.poolBuyAfter).toBe(true);
    const e = migrateEntry(tx, MINT, POOL, 0n)!;
    expect(e.bundledTokens).toBe(bought);
    expect(e.open!.price).toBeCloseTo(Number(Q0) / 1e9 / (Number(B0) / 1e6), 12);
    expect(e.open!.price).toBeLessThan(e.reach.price);
  });

  it('completion buy + pool buy in one tx: open is unknown', () => {
    const up = txUpdate(
      [...pumpIx('Buy'), ...pumpIx('MigrateV2'), ...swapIx('Buy')],
      [
        { mint: MINT, owner: POOL, amount: '200000000000000' },
        { mint: WSOL_MINT, owner: POOL, amount: '86000000000' },
        { mint: MINT, owner: CALLER, amount: '9000000000000' },
      ],
    );
    const tx = extractMigrateTx(up)!;
    expect(tx).toMatchObject({ curveBuyBefore: true, poolBuyAfter: true });
    expect(migrateEntry(tx, MINT, POOL, 0n)!.open).toBeNull();
  });

  it('a migrate on an already migrated curve is ok but not landed (live no-op)', () => {
    const bal = [
      { mint: MINT, owner: 'curve', amount: '0' },
      { mint: MINT, owner: POOL, amount: '206900000000000' },
      { mint: WSOL_MINT, owner: POOL, amount: '19142067' },
    ];
    const noop = extractMigrateTx(txUpdate([...pumpIx('Migrate'), 'Program log: Bonding curve already migrated'], bal, bal))!;
    expect(noop).toMatchObject({ ok: true, landed: false, mint: MINT });
    const winner = extractMigrateTx(txUpdate(pumpIx('MigrateV2'), bal, [{ mint: MINT, owner: 'curve', amount: '206900000000000' }]))!;
    expect(winner).toMatchObject({ ok: true, landed: true });
  });

  it('a failed attempt parses as ok=false; non-migrate txs are ignored', () => {
    const failed = extractMigrateTx(txUpdate(pumpIx('MigrateV2'), [{ mint: MINT, owner: 'curve', amount: '1' }], [], { InstructionError: [0, { Custom: 6005 }] }))!;
    expect(failed).toMatchObject({ ok: false, landed: false, mint: MINT });
    expect(extractMigrateTx(txUpdate(pumpIx('Buy'), []))).toBeNull();
  });
});

describe('MintResolver', () => {
  const mint = 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA';
  const curve = deriveBondingCurvePda(mint)!;

  it('applies at once for known curves', () => {
    const r = new MintResolver<number>(async () => []);
    r.noteMint(mint);
    const got: Array<[string, number]> = [];
    r.resolve(curve, null, 1, (m, i) => void got.push([m, i]));
    expect(got).toEqual([[mint, 1]]);
  });

  it('queues updates behind a lookup and replays them in order', async () => {
    let release: (v: string[]) => void = () => {};
    const r = new MintResolver<number>(() => new Promise((res) => (release = res)), {}, async () => {});
    const got: number[] = [];
    r.resolve(curve, 'sig', 1, (_m, i) => void got.push(i));
    r.resolve(curve, 'sig2', 2, (_m, i) => void got.push(i));
    expect(got).toEqual([]);
    release(['Other1111111111111111111111111111111111111', mint]);
    await new Promise((res) => setTimeout(res, 0));
    expect(got).toEqual([1, 2]);
    expect(r.mintOf(curve)).toBe(mint);
  });

  it('drops unknown curves without a signature, and remembers misses', async () => {
    let calls = 0;
    const r = new MintResolver<number>(async () => (calls++, []), { retries: 2 }, async () => {});
    r.resolve(curve, null, 1, () => {});
    expect(calls).toBe(0);
    r.resolve(curve, 'sig', 1, () => {});
    await new Promise((res) => setTimeout(res, 0));
    expect(calls).toBe(2);
    r.resolve(curve, 'sig', 1, () => {});
    expect(calls).toBe(2);
  });
});

describe('PaperTracker', () => {
  it('re-marks at horizons and records open + reach round trips', async () => {
    const tracks: PaperTrack[] = [];
    const scheduled: Array<() => void> = [];
    const p = new PaperTracker(
      {
        readMark: async () => ({ price: 2e-7, quoteSol: 1e9 }),
        sink: (t) => void tracks.push(t),
        schedule: (_ms, fn) => void scheduled.push(fn),
      },
      { horizonsSec: [30, 60], sizeSol: 0.1 },
    );
    p.track('M', { poolAddress: 'P', slot: 1, open: { price: 1e-7, quoteSol: 1e9 }, reach: { price: 1.5e-7, quoteSol: 1e9 } });
    expect(p.pendingCount).toBe(1);
    scheduled.forEach((fn) => fn());
    await new Promise((res) => setTimeout(res, 0));
    expect(tracks).toHaveLength(1);
    const h = tracks[0]!.horizons[0]!;
    expect(h.pnlOpenPct!).toBeGreaterThan(90);
    expect(h.pnlReachPct!).toBeGreaterThan(25);
    expect(h.pnlReachPct!).toBeLessThan(h.pnlOpenPct!);
    expect(p.pendingCount).toBe(0);
  });

  it('a track without an open mark still records reach', async () => {
    const tracks: PaperTrack[] = [];
    const p = new PaperTracker(
      { readMark: async () => ({ price: 1e-7, quoteSol: 1e9 }), sink: (t) => void tracks.push(t), schedule: (_ms, fn) => fn() },
      { horizonsSec: [30] },
    );
    p.track('M', { poolAddress: 'P', slot: 1, open: null, reach: { price: 1e-7, quoteSol: 1e9 } });
    await new Promise((res) => setTimeout(res, 0));
    expect(tracks[0]!.horizons[0]).toMatchObject({ pnlOpenPct: null });
    expect(tracks[0]!.horizons[0]!.pnlReachPct).not.toBeNull();
  });
});

