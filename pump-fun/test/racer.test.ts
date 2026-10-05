import { describe, expect, it } from 'vitest';
import { completionCostSol, curveProgress, evaluateFire, paperRoundTripPct } from '../src/racer/economics.ts';
import { RacerTracker, type TrackerEvent } from '../src/racer/tracker.ts';
import { PaperTracker, type PaperTrack } from '../src/racer/paper.ts';
import { MintResolver } from '../src/racer/mintResolver.ts';
import { SlotEntryTracker, buyerSignatures, decodeTokenAmount, handleTrackerVaultWatch, slotEntries } from '../src/racer/slotEntry.ts';
import { median, txExecutionCostSol } from '../src/racer/costs.ts';
import { buildRacerRequest, RacerStream, routeUpdate, REAL_TOKEN_TOP_OFFSET, type CurveUpdate } from '../src/racer/stream.ts';
import { extractMigrateTx, instructionTrace, isThinPool, migrateEntry, poolOf } from '../src/racer/migrateTx.ts';
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

describe('thin-pool coverage gate', () => {
  it('uses strict observed-depth comparison at the configured boundary', () => {
    expect(isThinPool({ price: 1, quoteSol: 9.999 }, 10)).toBe(true);
    expect(isThinPool({ price: 1, quoteSol: 10 }, 10)).toBe(false);
    expect(isThinPool(null, 10)).toBe(false);
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
  it('subscribes watched pools and reports both successful and failed S+1 buys', () => {
    const req = buildRacerRequest(1, [], ['pool']) as { transactions: Record<string, { accountInclude: string[]; accountRequired: string[] }> };
    expect(req.transactions.poolBuys).toMatchObject({ accountInclude: ['pool'], accountRequired: [PROGRAM_IDS.PUMP_SWAP] });
    const seen: Array<{ signature: string; poolAddress: string; slot: number; ok: boolean; buyerOwner: string | null; quoteVolumeSol: number | null; networkFeeSol: number | null }> = [];
    const update = (signature: string, err: unknown, withBalance = false) => ({ transaction: { slot: 101, transaction: {
      signature, meta: { err, fee: 5000, logMessages: [
        `Program ${PROGRAM_IDS.PUMP_SWAP} invoke [1]`, 'Program log: Instruction: Buy',
        `Program ${PROGRAM_IDS.PUMP_SWAP} success`,
      ], preTokenBalances: withBalance ? [
        { mint: WSOL_MINT, owner: 'pool', uiTokenAmount: { amount: '1000000000' } },
        { mint: 'base', owner: 'buyer', uiTokenAmount: { amount: '0' } },
      ] : [], postTokenBalances: withBalance ? [
        { mint: WSOL_MINT, owner: 'pool', uiTokenAmount: { amount: '2000000000' } },
        { mint: 'base', owner: 'buyer', uiTokenAmount: { amount: '100' } },
      ] : [] },
      transaction: { message: { accountKeys: ['payer', 'pool', PROGRAM_IDS.PUMP_SWAP] } },
    } } });
    const handlers = { onCurve: () => {}, onMigrateTx: () => {}, onLaunch: () => {},
      isWatchedPool: (pool: string) => pool === 'pool', onPoolBuy: (attempt: typeof seen[number]) => void seen.push(attempt) };
    routeUpdate(update('success', null, true), handlers);
    routeUpdate(update('failure', { InstructionError: [0, 'oops'] }), handlers);
    expect(seen).toEqual([
      { signature: 'success', poolAddress: 'pool', slot: 101, ok: true, buyerOwner: 'buyer', quoteVolumeSol: 1, networkFeeSol: 0.000005 },
      { signature: 'failure', poolAddress: 'pool', slot: 101, ok: false, buyerOwner: null, quoteVolumeSol: null, networkFeeSol: 0.000005 },
    ]);
  });
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

  it('arms at admission, keeps a migrated watch for pricing, and releases closed curves', () => {
    let now = 0;
    const watches = new SlotEntryTracker({ now: () => now, ttlMs: 100_000 });
    const t = new RacerTracker(opts, (event) => handleTrackerVaultWatch(event,
      (mint) => void watches.arm(mint, [`vault-${mint}`], true),
      (mint) => void watches.release(mint)), () => now);
    t.onCurve('M', 'C', standardCurve(tokensForCost.admit), 1);
    expect(watches.isWatched('vault-M')).toBe(true);
    t.onMigrate('M', 2);
    expect(watches.isWatched('vault-M')).toBe(true);
    watches.release('M'); // worker releases after pricing the migrated pool
    t.onCurve('N', 'C', standardCurve(tokensForCost.admit), 3);
    now = 60_000;
    t.sweep();
    expect(watches.watched()).toEqual([]);
  });

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

  it('ignores late curve writes for an already migrated mint', () => {
    const { t, events } = setup();
    t.onMigrate('M', 10);
    t.onCurve('M', 'C', standardCurve(0n, { complete: true, realSolReserves: 85_000_000_000n }), 9);
    expect(events.map((e) => e.kind)).toEqual(['migrated']);
    expect(t.size).toBe(0);
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
    // A loser bundling a pool buy moves balances but is still not landed.
    const loserBuy = extractMigrateTx(
      txUpdate([...pumpIx('MigrateV2'), 'Program log: Bonding curve already migrated', ...swapIx('Buy')], [{ mint: MINT, owner: CALLER, amount: '5' }]),
    )!;
    expect(loserBuy).toMatchObject({ ok: true, landed: false, poolBuyAfter: true });
  });

  it('a non-WSOL-quote migrate resolves the mint via its curve PDA and reports the quote mint', () => {
    const mint = 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA';
    const quote = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb';
    const curvePda = deriveBondingCurvePda(mint)!;
    const tx = extractMigrateTx(
      txUpdate(
        pumpIx('MigrateV2'),
        [
          { mint, owner: curvePda, amount: '0' },
          { mint, owner: 'PoolX', amount: '206900000000000' },
          { mint: quote, owner: 'PoolX', amount: '5000000' },
        ],
        [{ mint, owner: curvePda, amount: '206900000000000' }, { mint: quote, owner: 'Other', amount: '1' }],
      ),
    )!;
    expect(tx.mint).toBe(mint);
    expect(poolOf(tx, mint)).toEqual({ poolOwner: 'PoolX', quoteMint: quote });
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

  it('flushes queued updates when the mint is learned mid-lookup (migrate before lookup)', () => {
    const r = new MintResolver<number>(() => new Promise(() => {}), {}, async () => {});
    const got: number[] = [];
    r.resolve(curve, 'sig', 1, (_m, i) => void got.push(i));
    r.resolve(curve, 'sig', 2, (_m, i) => void got.push(i));
    r.noteMint(mint);
    expect(got).toEqual([1, 2]);
    r.resolve(curve, 'sig', 3, (_m, i) => void got.push(i));
    expect(got).toEqual([1, 2, 3]);
  });

  it('retries a missed curve after the cooldown', async () => {
    let now = 0;
    let calls = 0;
    const r = new MintResolver<number>(async () => (calls++, []), { retries: 1, missCooldownMs: 1_000 }, async () => {}, () => now);
    r.resolve(curve, 'sig', 1, () => {});
    await new Promise((res) => setTimeout(res, 0));
    r.resolve(curve, 'sig', 1, () => {});
    expect(calls).toBe(1);
    now = 1_001;
    r.resolve(curve, 'sig', 1, () => {});
    expect(calls).toBe(2);
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
  it('does not use the trigger mark when the simulated landed exit is unreadable', async () => {
    const tracks: PaperTrack[] = [];
    const marks = [{ price: 2e-7, quoteSol: 80 }, null];
    const p = new PaperTracker({ readMark: async () => marks.shift() ?? null,
      sink: (t) => void tracks.push(JSON.parse(JSON.stringify(t))), schedule: (_ms, fn) => fn() },
    { horizonsSec: [30], exitLandMs: 450 });
    p.track('M', { poolAddress: 'P', slot: 1, open: null, reach: { price: 1e-7, quoteSol: 80 },
      delayed: { slot1mid: { price: 1e-7, quoteSol: 80 } }, entryQuality: 'verified' });
    await new Promise((r) => setTimeout(r, 0));
    expect(tracks.at(-1)!.horizons[0]).toMatchObject({ exitPrice: null, pnl: { slot1mid: null } });
  });
  it('persists at entry and after each horizon; records open + reach round trips', async () => {
    const tracks: PaperTrack[] = [];
    const snapshots: string[] = [];
    let last: PaperTrack | null = null;
    const scheduled: Array<() => void> = [];
    const p = new PaperTracker(
      {
        readMark: async () => ({ price: 2e-7, quoteSol: 1e9 }),
        sink: (t) => void ((last = t), snapshots.push(JSON.stringify(t.horizons.map((h) => h.price)))),
        schedule: (_ms, fn) => void scheduled.push(fn),
      },
      { horizonsSec: [30, 60], sizeSol: 0.1 },
    );
    p.track('M', { poolAddress: 'P', slot: 1, open: { price: 1e-7, quoteSol: 1e9 }, reach: { price: 1.5e-7, quoteSol: 1e9 } });
    // Persisted at entry, before any horizon.
    expect(snapshots).toEqual(['[null,null]']);
    expect(p.pendingCount).toBe(1);
    scheduled[0]!();
    await new Promise((res) => setTimeout(res, 0));
    // The 30 s mark is persisted as soon as it lands.
    expect(snapshots.at(-1)).toBe('[2e-7,null]');
    expect(p.pendingCount).toBe(1);
    scheduled[1]!();
    await new Promise((res) => setTimeout(res, 0));
    expect(snapshots).toHaveLength(3);
    tracks.push(last!);
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


describe('realistic slot-delayed entries', () => {
  // Reach: 85 SOL effective quote over 206.9M tokens.
  const reachBase = 206_900_000_000_000n;
  const reach = { price: 85 / 206_900_000, quoteSol: 85 };

  it('decodes a token account amount at offset 64', () => {
    const buf = Buffer.alloc(165);
    buf.writeBigUInt64LE(123_456n, 64);
    expect(decodeTokenAmount(Uint8Array.from(buf))).toBe(123_456n);
    expect(decodeTokenAmount(new Uint8Array(10))).toBeNull();
  });

  it('prices +k at the end of slot migrate+k-1 along the constant product', () => {
    const S = 1000;
    const updates = [
      { slot: S, amount: reachBase }, // the migrate tx creates the vault
      { slot: S, amount: 180_000_000_000_000n }, // same-slot bundled buyers
      { slot: S + 1, amount: 100_000_000_000_000n },
      { slot: S + 5, amount: 50_000_000_000_000n }, // after the window
    ];
    const e = slotEntries(updates, S, reach, reachBase, [1, 2, 3]);
    const k = 85 * Number(reachBase);
    expect(e.slot1!.quoteSol).toBeCloseTo(k / 180_000_000_000_000, 6);
    expect(e.slot1!.price).toBeCloseTo(k / 180_000_000_000_000 / 180_000_000, 15);
    expect(e.slot2!.quoteSol).toBeCloseTo(k / 100_000_000_000_000, 6);
    expect(e.slot3).toEqual(e.slot2); // nothing in S+2
    expect(e.slot1!.price).toBeGreaterThan(reach.price);
  });

  it('does not invent a delayed entry when the migration-slot vault anchor is absent', () => {
    const e = slotEntries([], 1000, reach, reachBase, [1]);
    expect(e).toEqual({ slot1: null, slot1mid: null });
    const laterOnly = slotEntries([{ slot: 1001, amount: reachBase / 2n }], 1000, reach, reachBase, [1, 2]);
    expect(laterOnly).toEqual({ slot1: null, slot1mid: null, slot2: null, slot2mid: null });
    // A BR5JMi/RFZRKF-sized reach mark must not leak into +1 paper P&L.
    const outsizedReach = { price: 0.00144, quoteSol: 5_000 };
    expect(slotEntries([{ slot: 1001, amount: reachBase / 2n }], 1000, outsizedReach, reachBase, [1]).slot1mid).toBeNull();
  });

  it('uses an observed flat migrate slot as the +1 first entry', () => {
    const e = slotEntries([{ slot: 1000, amount: reachBase, signature: 'migrate' }], 1000, reach, reachBase, [1]);
    expect(e.slot1!.price).toBeCloseTo(reach.price, 15);
  });

  it('freezes same-slot depletion at observation time without future writes', () => {
    const t = new SlotEntryTracker();
    t.arm('M', ['vault'], true);
    expect(t.observedSameSlotDepletionPct('M', 1000, 'migrate', reachBase)).toBeNull();
    t.onVault('vault', reachBase, 1000, 'migrate');
    const atDecision = t.observedSameSlotDepletionPct('M', 1000, 'migrate', reachBase);
    t.onVault('vault', reachBase / 2n, 1000, 'later-buy');
    expect(atDecision).toBe(0);
    expect(t.observedSameSlotDepletionPct('M', 1000, 'migrate', reachBase)).toBe(50);
  });

  it('tracks watches per mint, and take() prices and releases them', () => {
    let now = 0;
    const t = new SlotEntryTracker({ ttlMs: 1_000, now: () => now });
    expect(t.arm('M', ['v1', 'v2'], true)).toBe(true);
    expect(t.arm('M', ['v1', 'v2'], false)).toBe(false);
    expect(t.watched().sort()).toEqual(['v1', 'v2']);
    t.onVault('v2', reachBase, 1000, 'migrate');
    t.onVault('v2', 150_000_000_000_000n, 1000);
    t.onVault('vX', 1n, 1000);
    const got = t.take('M', 1000, 'migrate', reach, reachBase, [1])!;
    expect(got).toMatchObject({ early: true, quality: 'verified', updates: 2 });
    expect(got.sameSlotDepletionPct).toBeCloseTo((1 - 150 / 206.9) * 100, 5);
    expect(got.entries.slot1!.quoteSol).toBeCloseTo((85 * Number(reachBase)) / 150_000_000_000_000, 6);
    expect(t.watched()).toEqual([]);
    t.arm('N', ['v3'], false);
    now = 2_000;
    expect(t.sweep()).toBe(true);
    expect(t.isWatched('v3')).toBe(false);
  });

  it('quarantines missed-anchor and late-armed entries', () => {
    const t = new SlotEntryTracker();
    t.arm('missing', ['v1'], true);
    t.onVault('v1', reachBase / 2n, 1001, 'buyer');
    expect(t.take('missing', 1000, 'migrate', reach, reachBase, [1])!).toMatchObject({
      quality: 'missing-anchor', entries: { slot1: null, slot1mid: null },
    });
    t.arm('late', ['v2'], false);
    t.onVault('v2', reachBase, 1000, 'migrate');
    expect(t.take('late', 1000, 'migrate', reach, reachBase, [1])!.quality).toBe('late-armed');
  });

  it('routes watched vault writes before curve decoding', () => {
    const seen: Array<[string, number | null]> = [];
    const kind = routeUpdate(
      { account: { slot: '7', account: { pubkey: 'VaultKey', owner: PROGRAM_IDS.TOKEN_2022, data: new Uint8Array(165) } } },
      { onCurve: () => {}, onMigrateTx: () => {}, onLaunch: () => {}, isWatched: (a) => a === 'VaultKey', onWatched: (a, _d, s) => void seen.push([a, s]) },
    );
    expect(kind).toBe('watched');
    expect(seen).toEqual([['VaultKey', 7]]);
    const req = buildRacerRequest(1, ['VaultKey']) as { accounts: Record<string, { account: string[] }> };
    expect(req.accounts.vaults!.account).toEqual(['VaultKey']);
  });

  it('paper charges live cost to the realistic legs only', async () => {
    const tracks: PaperTrack[] = [];
    const p = new PaperTracker(
      { readMark: async () => ({ price: 1e-7, quoteSol: 1e9 }), sink: (t) => void tracks.push(t), schedule: (_ms, fn) => fn() },
      { horizonsSec: [30], sizeSol: 0.1, liveCostSol: 0.002 },
    );
    const flat = { price: 1e-7, quoteSol: 1e9 };
    p.track('M', { poolAddress: 'P', slot: 1, open: flat, reach: flat, delayed: { slot1: flat, slot2: null }, armed: true, elapsedMs: 2_500 });
    await new Promise((res) => setTimeout(res, 0));
    const h = tracks[0]!.horizons[0]!;
    expect(h.pnl.slot1!).toBeCloseTo(h.pnl.reach! - 2, 6);
    expect(h.pnl.slot2).toBeNull();
    expect(tracks[0]!.armed).toBe(true);
  });
});

describe('paper realism', () => {
  const flat = { price: 1e-7, quoteSol: 1e9 };

  it('caps a sell at the SOL really in the quote vault (drained pool)', () => {
    const full = paperRoundTripPct(flat, { price: 2e-7, quoteSol: 1e9 }, 0.1)!;
    const drained = paperRoundTripPct(flat, { price: 2e-7, quoteSol: 1e9, realQuoteSol: 0.001 }, 0.1)!;
    expect(full).toBeGreaterThan(90);
    expect(drained).toBeCloseTo(-99, 0);
  });

  it('scores a gone pool as -100% for every entry (never drops the sample)', async () => {
    const tracks: PaperTrack[] = [];
    const p = new PaperTracker(
      { readMark: async () => 'gone', sink: (t) => void tracks.push(t), schedule: (_ms, fn) => fn() },
      { horizonsSec: [30], sizeSol: 0.1, liveCostSol: 0.002 },
    );
    p.track('M', { poolAddress: 'P', slot: 1, open: flat, reach: flat, delayed: { slot1: flat, slot1mid: flat }, armed: true });
    await new Promise((res) => setTimeout(res, 0));
    const h = tracks.at(-1)!.horizons[0]!;
    expect(h.rugged).toBe(true);
    expect(h.pnl).toEqual({ open: -100, reach: -100, slot1: -102, slot1mid: -102 });
  });

  it('realistic legs exit where the sell lands; reach keeps the horizon mid', async () => {
    const tracks: PaperTrack[] = [];
    const marks = [{ price: 2e-7, quoteSol: 1e9 }, { price: 1e-7, quoteSol: 1e9 }];
    const delays: number[] = [];
    const p = new PaperTracker(
      { readMark: async () => marks.shift()!, sink: (t) => void tracks.push(t), schedule: (ms, fn) => (delays.push(ms), fn()) },
      { horizonsSec: [30], sizeSol: 0.1, liveCostSol: 0, exitLandMs: 450 },
    );
    p.track('M', { poolAddress: 'P', slot: 1, open: null, reach: flat, delayed: { slot1mid: flat } });
    await new Promise((res) => setTimeout(res, 0));
    await new Promise((res) => setTimeout(res, 0));
    const h = tracks.at(-1)!.horizons[0]!;
    expect(delays).toContain(450);
    expect(h.pnl.reach!).toBeGreaterThan(90); // sold at the 2x horizon mid
    expect(h.pnl.slot1mid!).toBeLessThan(0); // sold where it landed, back at 1x
    expect(h.exitPrice).toBe(1e-7);
  });

  it('a measured live cost re-costs marked horizons and is re-sunk', async () => {
    const tracks: PaperTrack[] = [];
    const scheduled: Array<() => void> = [];
    const p = new PaperTracker(
      { readMark: async () => flat, sink: (t) => void tracks.push(JSON.parse(JSON.stringify(t))), schedule: (_ms, fn) => void scheduled.push(fn) },
      { horizonsSec: [30, 60], sizeSol: 0.1, liveCostSol: 0.002 },
    );
    p.track('M', { poolAddress: 'P', slot: 1, open: null, reach: flat, delayed: { slot1mid: flat } });
    scheduled[0]!();
    await new Promise((res) => setTimeout(res, 0));
    const before = tracks.at(-1)!.horizons[0]!.pnl.slot1mid!;
    p.setLiveCost('M', 0.012);
    const after = tracks.at(-1)!;
    expect(after.liveCostSource).toBe('measured');
    expect(after.horizons[0]!.pnl.slot1mid!).toBeCloseTo(before - 10, 6);
  });

  it('measures fee + tips from a buyer tx, ignoring token accounts and the payer', () => {
    const cost = txExecutionCostSol({
      meta: {
        err: null,
        fee: 105_000,
        preBalances: [1_000_000_000, 5_000, 2_039_280, 0],
        postBalances: [900_000_000, 1_005_000, 4_078_560, 0],
        preTokenBalances: [{ accountIndex: 2 }],
        postTokenBalances: [{ accountIndex: 2 }],
      },
    });
    // fee 105k + tip 1M lamports; the token account's lamport change is excluded.
    expect(cost).toBeCloseTo(0.001105, 9);
    expect(txExecutionCostSol({ meta: { err: { x: 1 }, fee: 5000, preBalances: [], postBalances: [] } })).toBeNull();
    expect(median([3, 1, 2])).toBe(2);
    expect(median([])).toBeNull();
  });

  it('prices the median landing position within slot +1 and lists its buyers', () => {
    const reachBase = 206_900_000_000_000n;
    const reach = { price: 85 / 206_900_000, quoteSol: 85 };
    const S = 500;
    const updates = [
      { slot: S, amount: reachBase, signature: 'mig' },
      { slot: S + 1, amount: 200_000_000_000_000n, signature: 'b1' },
      { slot: S + 1, amount: 190_000_000_000_000n, signature: 'b2' },
      { slot: S + 1, amount: 195_000_000_000_000n, signature: 's1' }, // a sell
      { slot: S + 1, amount: 180_000_000_000_000n, signature: 'b3' },
    ];
    const e = slotEntries(updates, S, reach, reachBase, [1]);
    // States in slot +1: [206.9 (first), 200, 190, 195, 180] → median index 2 → 190.
    const k = 85 * Number(reachBase);
    expect(e.slot1!.quoteSol).toBeCloseTo(k / Number(reachBase), 6);
    expect(e.slot1mid!.quoteSol).toBeCloseTo(k / 190_000_000_000_000, 6);
    expect(buyerSignatures(updates, S + 1)).toEqual(['b1', 'b2', 'b3']);
  });
});

describe('racer stream gaps', () => {
  it('reports a gap on SDK stream errors and on failed watch updates', async () => {
    const gaps: string[] = [];
    let onError: ((e: unknown) => void) | undefined;
    const stream = new RacerStream(
      {
        endpoint: 'https://example.invalid',
        token: 't',
        bandValues: 1,
        onGap: (r) => void gaps.push(r),
        subscribeFn: async (_c, _r, _d, err) => {
          onError = err as (e: unknown) => void;
          return { cancel() {}, write: async () => { throw new Error('write rejected'); } };
        },
      },
      { onCurve: () => {}, onMigrateTx: () => {}, onLaunch: () => {} },
    );
    stream.start();
    await new Promise((res) => setTimeout(res, 0));
    onError?.(new Error('boom'));
    stream.setWatches(['V'], []);
    await new Promise((res) => setTimeout(res, 0));
    stream.stop();
    expect(gaps).toEqual(['stream error', 'watch update failed']);
  });

  it('detects gaps the SDK may not report: heartbeat slot jumps and frame silence', async () => {
    const gaps: string[] = [];
    let push: ((d: unknown) => void) | undefined;
    let now = 1_000;
    const stream = new RacerStream(
      {
        endpoint: 'https://example.invalid',
        token: 't',
        bandValues: 1,
        now: () => now,
        onGap: (r) => void gaps.push(r),
        subscribeFn: async (_c, _r, onData) => {
          push = onData as (d: unknown) => void;
          return { cancel() {} };
        },
      },
      { onCurve: () => {}, onMigrateTx: () => {}, onLaunch: () => {} },
    );
    stream.start();
    await new Promise((res) => setTimeout(res, 0));
    const tick = (slot: number, dt = 400) => {
      now += dt;
      push!({ slot: { slot: String(slot) } });
    };
    tick(100);
    tick(101);
    tick(105); // skipped leader slots: normal
    expect(gaps).toEqual([]);
    tick(140); // silent internal reconnect without replay
    expect(gaps).toEqual(['slot jump 105 -> 140']);
    tick(141, 5_000);
    expect(gaps.at(-1)).toBe('frame silence 5000 ms');
    expect(stream.stats.gaps).toBe(2);
    stream.stop();
  });
});
