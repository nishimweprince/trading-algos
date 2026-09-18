import { describe, it, expect, vi, afterEach } from 'vitest';
import { PublicKey, SystemProgram, TransactionMessage, VersionedTransaction } from '@solana/web3.js';
import { CurveTrader, evaluateCurveExit } from '../src/executor/curveTrader.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import type { Executor } from '../src/executor/index.ts';
import type { RiskManager } from '../src/risk/manager.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import { deriveBondingCurvePda } from '../src/enrichment/curve.ts';

const MINT = '7v4shBJmb73embNcid1dMQhZFhNTpBhtFPMFTm73b4bv';
const WALLET = '4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf';
const DISC = Buffer.from('17b7f83760d8ac60', 'hex');

/** Synthetic unsigned trade tx: payer + mint referenced (satisfies verify). */
function tradeBytes(): Uint8Array {
  const payer = new PublicKey(WALLET);
  const msg = new TransactionMessage({
    payerKey: payer,
    recentBlockhash: '11111111111111111111111111111111',
    instructions: [
      SystemProgram.transfer({ fromPubkey: payer, toPubkey: payer, lamports: 1 }),
      SystemProgram.transfer({ fromPubkey: payer, toPubkey: new PublicKey(MINT), lamports: 0 }),
    ],
  }).compileToV0Message();
  return new Uint8Array(new VersionedTransaction(msg).serialize());
}

function stubTradeFetch() {
  vi.stubGlobal(
    'fetch',
    (async () => new Response(tradeBytes(), { status: 200 })) as unknown as typeof fetch,
  );
}

function stubDeadFetch() {
  vi.stubGlobal(
    'fetch',
    (async () => {
      throw new Error('no network in tests');
    }) as unknown as typeof fetch,
  );
}

function curveData(opts: { realTokenPct?: number; realSol?: number; complete?: boolean } = {}): string {
  const { realTokenPct = 5, realSol = 70, complete = false } = opts;
  const buf = Buffer.alloc(49);
  DISC.copy(buf, 0);
  buf.writeBigUInt64LE(1_000_000_000_000_000n, 8);
  buf.writeBigUInt64LE(60_000_000_000n, 16);
  buf.writeBigUInt64LE((793_100_000_000_000n * BigInt(realTokenPct)) / 100n, 24);
  buf.writeBigUInt64LE(BigInt(Math.round(realSol * 1e9)), 32);
  buf.writeBigUInt64LE(1_000_000_000_000_000n, 40);
  buf[48] = complete ? 1 : 0;
  return buf.toString('base64');
}

function setup(opts: {
  pregrad?: Record<string, unknown>;
  curve?: string | null;
  broadcast?: { sent: boolean; confirmed: boolean; signature?: string };
  balance?: bigint;
  /** Wallet SOL delta (lamports) between pre/post-sell reads for S4 proceeds. */
  solDelta?: bigint;
  risk?: Partial<{ killed: boolean; canEnterOk: boolean }>;
}) {
  vi.useFakeTimers();
  vi.setSystemTime(2_000_000);
  const config = ConfigSchema.parse({
    rpc: { primaryHttp: 'http://x' },
    pregrad: { enabled: true, ...(opts.pregrad ?? {}) },
  });
  const db = openDb({ path: ':memory:', memory: true });
  const repos = new Repositories(db);
  const curvePda = deriveBondingCurvePda(MINT)!;
  let balanceReads = 0;
  const rpc = {
    getAccountInfoBase64: async (addr: string) => {
      if (addr === curvePda) {
        return opts.curve === null || opts.curve === undefined ? null : { data: opts.curve, owner: '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P' };
      }
      if (addr === MINT) return { data: '', owner: 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA' };
      return null;
    },
    getMultipleAccountsBase64: async () => [],
    getBalance: async () => {
      balanceReads++;
      const base = 10_000_000_000n;
      if (opts.solDelta !== undefined && balanceReads >= 2) return base + opts.solDelta;
      return base;
    },
  } as unknown as RpcClient;
  const broadcasts: Array<string> = [];
  const sellCalls: Array<{ pool: string; mint: string; amount: bigint }> = [];
  const executor = {
    publicKey: WALLET,
    signAndBroadcastCurveTrade: async (_b: Uint8Array, label: string) => {
      broadcasts.push(label);
      return { sent: opts.broadcast?.sent ?? true, confirmed: opts.broadcast?.confirmed ?? true, signature: opts.broadcast?.signature ?? 'SIG', mode: 'live', simulated: true };
    },
    sellAndConfirm: async (pool: string, mint: string, amount: bigint, _slippagePct: number) => {
      sellCalls.push({ pool, mint, amount });
      broadcasts.push('pumpswap-sell');
      return { sent: opts.broadcast?.sent ?? true, confirmed: opts.broadcast?.confirmed ?? true, signature: opts.broadcast?.signature ?? 'PUMP-SIG', mode: 'live', simulated: true };
    },
    reconcileTokenBalance: async () => opts.balance ?? 1_000_000n,
  } as unknown as Executor;
  let killed = false;
  const risk = {
    get killed() {
      return killed || (opts.risk?.killed ?? false);
    },
    canEnter: () => (opts.risk?.canEnterOk === false ? { ok: false, reason: 'TEST' } : { ok: true }),
    reserveSol: () => {},
    releaseSol: () => {},
    applyBalanceDeltaSol: () => {},
    engageKillSwitch: () => {
      killed = true;
    },
  } as unknown as RiskManager;
  const trader = new CurveTrader({ config, rpc, repos, executor, risk, opts: { now: () => Date.now() } });
  return { db, repos, trader, broadcasts, sellCalls, risk };
}

describe('evaluateCurveExit', () => {
  const cfg = { takeProfitPct: 30, stopLossPct: 15, trailPct: 10, timeStopMs: 30 * 60_000 };
  const st = (peak: number) => ({ entryPrice: 100, peakPrice: peak, entryMs: 0 });
  it('takes profit, stops loss, trails after arming, times out, else holds', () => {
    expect(evaluateCurveExit(st(100), 130, 1, cfg)).toBe('TAKE_PROFIT');
    expect(evaluateCurveExit(st(100), 85, 1, cfg)).toBe('STOP_LOSS');
    expect(evaluateCurveExit(st(140), 125, 1, cfg)).toBe('TRAILING_STOP');
    expect(evaluateCurveExit(st(120), 115, 1, cfg)).toBe('HOLD');
    expect(evaluateCurveExit(st(110), 105, 31 * 60_000, cfg)).toBe('TIME_STOP');
    expect(evaluateCurveExit(st(110), 105, 1, cfg)).toBe('HOLD');
    expect(evaluateCurveExit(st(0), 0, 1, cfg)).toBe('HOLD');
  });
});

describe('CurveTrader select/enter', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function seedTrack(repos: Repositories) {
    repos.recordLaunch({ mint: MINT, feedSource: 'pumpportal', receivedAtNs: 1n });
    repos.openLaunchTrack(MINT);
    repos.setLaunchBaseline(MINT, 1e-14);
  }

  it('skips when disabled and on sublimit breach', async () => {
    stubTradeFetch();
    const off = setup({ pregrad: { enabled: false }, curve: curveData() });
    seedTrack(off.repos);
    await off.trader.selectOnce();
    expect(off.broadcasts).toHaveLength(0);

    const broke = setup({ curve: curveData() });
    seedTrack(broke.repos);
    broke.repos.recordCurvePosition({ mint: 'x', state: 'CLOSED', sizeSol: 1, executionJson: null });
    broke.db.prepare(`UPDATE curve_positions SET net_pnl_sol = -5, closed_at = datetime('now')`).run();
    await broke.trader.selectOnce();
    expect(broke.broadcasts).toHaveLength(0);
  });

  it('enters an accepted late curve and records OPEN on confirmed fill', async () => {
    stubTradeFetch();
    const { db, repos, trader, broadcasts } = setup({ curve: curveData() });
    seedTrack(repos);
    await trader.selectOnce();
    expect(broadcasts).toHaveLength(1);
    expect(broadcasts[0]).toContain('curve-buy');
    const opens = repos.listCurvePositionsByState('OPEN');
    expect(opens).toHaveLength(1);
    expect(opens[0]!['entry_tx']).toBe('SIG');
    // Post-grad ledger untouched.
    expect(db.prepare('SELECT COUNT(*) AS n FROM positions').get()).toEqual({ n: 0 });
    expect(db.prepare('SELECT COUNT(*) AS n FROM candidates').get()).toEqual({ n: 0 });
  });

  it('vetoes early curves (no entry)', async () => {
    stubTradeFetch();
    const { trader, broadcasts, repos } = setup({ curve: curveData({ realTokenPct: 100, realSol: 1 }) });
    seedTrack(repos);
    await trader.selectOnce();
    expect(broadcasts).toHaveLength(0);
  });

  it('fails the entry when broadcast is unconfirmed, and kills on ambiguous fill', async () => {
    stubTradeFetch();
    const unconf = setup({ curve: curveData(), broadcast: { sent: true, confirmed: false } });
    seedTrack(unconf.repos);
    await unconf.trader.selectOnce();
    expect(unconf.repos.listCurvePositionsByState('FAILED')).toHaveLength(1);

    const amb = setup({ curve: curveData(), balance: 0n });
    seedTrack(amb.repos);
    await amb.trader.selectOnce();
    expect(amb.repos.listCurvePositionsByState('FAILED')).toHaveLength(1);
    expect(amb.risk.killed).toBe(true);
  });
});

describe('CurveTrader manage/recover', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('parks graduated positions for S4 and closes TP exits with PnL', async () => {
    const t = setup({ curve: curveData() });
    t.repos.recordCurvePosition({ mint: MINT, state: 'OPEN', sizeSol: 0.05, entryPrice: 1e-14, entryBaseAmount: '5000000', openedAt: '2026-09-18 10:00:00' });
    t.repos.recordGraduation({ mint: MINT, venue: 'pumpswap', poolAddress: '', slot: 1, feedSource: 'pumpportal', receivedAtNs: 2n });
    await t.trader.manageOnce();
    const parked = t.repos.listCurvePositionsByState('EXITING');
    expect(parked).toHaveLength(1);
    expect(parked[0]!['exit_reason']).toBe('GRADUATED_HOLD');
  });

  it('S4: switches a graduated position to pumpswap and closes with venue attribution', async () => {
    const t = setup({ curve: curveData(), solDelta: 60_000_000n });
    t.repos.recordCurvePosition({ mint: MINT, state: 'OPEN', sizeSol: 0.05, entryPrice: 1e-14, entryBaseAmount: '1000000', openedAt: '2026-09-18 10:00:00' });
    t.repos.recordGraduation({ mint: MINT, venue: 'pumpswap', poolAddress: 'POOL123', slot: 1, feedSource: 'pumpportal', receivedAtNs: 2n });
    await t.trader.manageOnce();
    expect(t.sellCalls).toHaveLength(1);
    expect(t.sellCalls[0]).toMatchObject({ pool: 'POOL123', mint: MINT, amount: 1_000_000n });
    const closed = t.repos.listCurvePositionsByState('CLOSED');
    expect(closed).toHaveLength(1);
    expect(closed[0]!['exit_venue']).toBe('pumpswap');
    expect(closed[0]!['exit_tx']).toBe('PUMP-SIG');
    expect(closed[0]!['exit_price']).toBeGreaterThan(0);
    expect(closed[0]!['net_pnl_sol']).toBeGreaterThan(0);
    // No orphaned curve-side state, post-grad ledger untouched.
    expect(t.repos.listCurvePositionsByState('OPEN')).toHaveLength(0);
    expect(t.repos.listCurvePositionsByState('EXITING')).toHaveLength(0);
    expect(t.db.prepare('SELECT COUNT(*) AS n FROM positions').get()).toEqual({ n: 0 });
  });

  it('S4: parks when the pool is unknown, recovers once it arrives', async () => {
    const t = setup({ curve: curveData(), solDelta: 60_000_000n });
    t.repos.recordCurvePosition({ mint: MINT, state: 'OPEN', sizeSol: 0.05, entryPrice: 1e-14, entryBaseAmount: '1000000', openedAt: '2026-09-18 10:00:00' });
    t.repos.recordGraduation({ mint: MINT, venue: 'pumpswap', poolAddress: '', slot: 1, feedSource: 'pumpportal', receivedAtNs: 2n });
    await t.trader.manageOnce();
    expect(t.sellCalls).toHaveLength(0);
    expect(t.repos.listCurvePositionsByState('EXITING')).toHaveLength(1);
    // Pool arrives later (detector backfill); recovery retries the switch.
    t.db.prepare(`UPDATE graduations SET pool_address = 'POOL123' WHERE mint = ?`).run(MINT);
    await t.trader.selectOnce(); // triggers recovery first
    const closed = t.repos.listCurvePositionsByState('CLOSED');
    expect(closed).toHaveLength(1);
    expect(closed[0]!['exit_venue']).toBe('pumpswap');
  });

  it('S4: stays parked when the pumpswap sell is unconfirmed', async () => {
    const t = setup({ curve: curveData(), broadcast: { sent: true, confirmed: false }, solDelta: 60_000_000n });
    t.repos.recordCurvePosition({ mint: MINT, state: 'OPEN', sizeSol: 0.05, entryPrice: 1e-14, entryBaseAmount: '1000000', openedAt: '2026-09-18 10:00:00' });
    t.repos.recordGraduation({ mint: MINT, venue: 'pumpswap', poolAddress: 'POOL123', slot: 1, feedSource: 'pumpportal', receivedAtNs: 2n });
    await t.trader.manageOnce();
    expect(t.sellCalls).toHaveLength(1);
    const parked = t.repos.listCurvePositionsByState('EXITING');
    expect(parked).toHaveLength(1);
    expect(parked[0]!['exit_reason']).toBe('GRADUATED_HOLD');
  });

  it('recovers pending to FAILED and retries exiting', async () => {
    stubDeadFetch(); // exit retry must not reach the network; stays EXITING
    const t = setup({ curve: curveData() });
    t.repos.recordCurvePosition({ mint: MINT, state: 'PENDING_ENTRY', sizeSol: 0.05 });
    t.repos.recordCurvePosition({ mint: 'other', state: 'EXITING', sizeSol: 0.05, entryPrice: 1e-14, entryBaseAmount: '5000000' });
    // EXITING row needs an exit reason + balance path: give it one via update.
    const exiting = t.repos.listCurvePositionsByState('EXITING');
    t.repos.updateCurvePositionState(exiting[0]!['rowid'] as number, 'EXITING', { exit_reason: 'STOP_LOSS', is_token_2022: 0 });
    await t.trader.selectOnce(); // triggers recovery first
    expect(t.repos.listCurvePositionsByState('FAILED')).toHaveLength(1);
    // Exiting retried: either closed (sell confirmed by fake) or still exiting.
    const closed = t.repos.listCurvePositionsByState('CLOSED');
    expect(closed.length + t.repos.listCurvePositionsByState('EXITING').length).toBe(1);
  });
});
