import { describe, expect, it, vi } from 'vitest';
import { WalletStream, extractWalletTx } from '../src/positions/walletStream.ts';
import { PositionManager } from '../src/positions/manager.ts';
import type { PricePoller, PriceTick, PoolRef } from '../src/positions/pricing.ts';
import { TypedBus } from '../src/core/bus.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { openDb } from '../src/persistence/db.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { PROGRAM_IDS } from '../src/core/constants.ts';
import type { PoolPricingRef, Position } from '../src/core/types.ts';
import type { Executor } from '../src/executor/index.ts';
import type { BroadcastResult } from '../src/executor/broadcaster.ts';

const WALLET = 'Wa11et1111111111111111111111111111111111111';
const OTHER = 'Other11111111111111111111111111111111111111';
const MINT = 'mint';

/** A LaserStream SubscribeUpdate as the SDK delivers it (strings for u64s). */
function update(opts: {
  signature: string;
  feePayer?: string;
  mint?: string;
  preRaw?: string | null;
  postRaw?: string;
  solPre?: string;
  solPost?: string;
  err?: unknown;
  slot?: string;
}) {
  const mint = opts.mint ?? MINT;
  const tb = (raw: string) => ({ accountIndex: 2, mint, owner: WALLET, programId: PROGRAM_IDS.TOKEN_2022, uiTokenAmount: { amount: raw, decimals: 6 } });
  return {
    transaction: {
      slot: opts.slot ?? '100',
      transaction: {
        signature: opts.signature,
        transaction: { message: { accountKeys: [opts.feePayer ?? WALLET, 'pool', 'ata'] } },
        meta: {
          err: opts.err,
          preBalances: [opts.solPre ?? '1000000000', '0', '0'],
          postBalances: [opts.solPost ?? '990000000', '0', '0'],
          preTokenBalances: opts.preRaw === null || opts.preRaw === undefined ? [] : [tb(opts.preRaw)],
          postTokenBalances: [tb(opts.postRaw ?? '5000000')],
        },
      },
    },
  };
}

describe('extractWalletTx', () => {
  it('reads signature, fee payer, SOL delta and per-mint token delta for our wallet', () => {
    const tx = extractWalletTx(update({ signature: 'S1', preRaw: '1000000', postRaw: '6000000' }), WALLET, 7)!;
    expect(tx.signature).toBe('S1');
    expect(tx.feePayer).toBe(WALLET);
    expect(tx.solDeltaLamports).toBe(-10_000_000n);
    expect(tx.tokenDeltas.get(MINT)).toEqual({ raw: 5_000_000n, decimals: 6, programId: PROGRAM_IDS.TOKEN_2022 });
    expect(tx.slot).toBe(100);
    expect(tx.err).toBeNull();
  });

  it('keeps the on-chain error', () => {
    expect(extractWalletTx(update({ signature: 'S2', err: { err: [1, 2] } }), WALLET, 0)!.err).toEqual({ err: [1, 2] });
  });
});

describe('WalletStream waits', () => {
  it('resolves a mint-increase wait on the next matching tx and ignores failed ones', async () => {
    const s = new WalletStream({ endpoint: '', wallet: WALLET });
    const w = s.waitForMintIncrease(MINT, 1_000);
    s.ingest(update({ signature: 'F', err: { err: 'x' } }));
    s.ingest(update({ signature: 'OK' }));
    expect((await w.promise)?.signature).toBe('OK');
  });

  it('answers a signature wait from recent history', async () => {
    const s = new WalletStream({ endpoint: '', wallet: WALLET });
    s.ingest(update({ signature: 'EARLY' }));
    expect((await s.waitForSignature('EARLY', 10))?.signature).toBe('EARLY');
  });

  it('times out to null and cancel() releases the waiter', async () => {
    const s = new WalletStream({ endpoint: '', wallet: WALLET });
    expect(await s.waitForSignature('NEVER', 5)).toBeNull();
    const w = s.waitForMintIncrease(MINT, 0);
    w.cancel();
    expect(await w.promise).toBeNull();
    expect(s.stats.waiters).toBe(0);
  });
});

// ── PositionManager under an RPC outage ──────────────────────────────────────

class FakePoller {
  handler: (t: PriceTick) => void = () => {};
  registered = new Set<string>();
  readPoolPricing = vi.fn(async (): Promise<PoolPricingRef | null> => pricing());
  setHandler(h: (t: PriceTick) => void) { this.handler = h; }
  register(r: PoolRef) { this.registered.add(r.mint); }
  unregister(m: string) { this.registered.delete(m); }
  async readOnce() { return null; }
  start() {}
  stop() { this.registered.clear(); }
  get size() { return this.registered.size; }
  get pollStats() { return { cycles: 0, overlapSkips: 0, deadlineExpired: 0, failures: 0, lastErr: null }; }
}

const pricing = (): PoolPricingRef => ({
  poolAddress: 'pool', baseMint: MINT, baseVault: 'bv', quoteVault: 'qv', baseDecimals: 6,
  baseReserve: 206_900_000_000_000n, quoteReserveLamports: 67_405_855_261n, virtualQuoteLamports: 17_584_505_324n,
});

const timedOut = (signature: string): BroadcastResult => ({
  mode: 'live', simulated: false, sent: true, confirmed: false, landingUnknown: true, signature, route: 'primary',
  sendErr: 'confirmation timeout', submittedAtMs: Date.now(), attempts: [{ route: 'primary', submittedAtMs: 1, sent: true, signature }],
});

const rateLimited = () => Promise.reject(new Error('HTTP 429'));

function harness(executor: Partial<Executor>, sentBuyResolveMs = 50) {
  const bus = new TypedBus();
  const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
  const poller = new FakePoller();
  const stream = new WalletStream({ endpoint: '', wallet: WALLET });
  const config = ConfigSchema.parse({
    mode: 'live',
    rpc: { primaryHttp: 'https://rpc.example' },
    execution: { sentBuyPollMs: 5, sentBuyResolveMs, sentBuyHardMaxMs: 2_000, adoptUntrackedDelayMs: 0 },
  });
  const mgr = new PositionManager({
    config, bus, repos, poller: poller as unknown as PricePoller, executor: executor as Executor, walletWatch: stream,
  });
  mgr.start();
  const updates: Position[] = [];
  bus.on('positionUpdate', (p) => updates.push(p));
  return { bus, mgr, stream, poller, updates };
}

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe('stuck-buy safety (2026-10-03 incident)', () => {
  it('adopts a timed-out buy from the wallet stream while every RPC read is 429', async () => {
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(async () => timedOut('BUY')),
      signatureStatus: vi.fn(rateLimited),
      readTokenBalance: vi.fn(rateLimited),
      reconcileTokenBalance: vi.fn(rateLimited),
      buildExitLadder: vi.fn(() => ({ refresh: vi.fn(async () => undefined) }) as never),
    };
    const { bus, mgr, stream, updates } = harness(executor, 10_000);
    bus.emit('openPosition', { mint: MINT, sizeSol: 0.01, highVolatility: false, pricing: pricing() });
    await wait(20);
    stream.ingest(update({ signature: 'BUY', postRaw: '24000000000', solPre: '100000000', solPost: '89000000' }));
    await wait(30);
    const open = updates.find((u) => u.state === 'OPEN');
    expect(open).toBeDefined();
    expect(updates.some((u) => u.state === 'FAILED')).toBe(false);
    mgr.stop();
  });

  it('does not declare a sent buy failed while the stream was down across the send', async () => {
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(async () => timedOut('BUY')),
      signatureStatus: vi.fn(rateLimited),
      readTokenBalance: vi.fn(rateLimited),
      reconcileTokenBalance: vi.fn(rateLimited),
      buildExitLadder: vi.fn(() => ({ refresh: vi.fn(async () => undefined) }) as never),
    };
    const { bus, mgr, updates } = harness(executor, 20);
    bus.emit('openPosition', { mint: MINT, sizeSol: 0.01, highVolatility: false, pricing: pricing() });
    await wait(120); // well past sentBuyResolveMs
    expect(updates.some((u) => u.state === 'FAILED')).toBe(false); // still resolving, not abandoned
    mgr.stop();
  });

  it('fails a sent buy on an on-chain error seen by the stream', async () => {
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(async () => timedOut('BUY')),
      signatureStatus: vi.fn(rateLimited),
      readTokenBalance: vi.fn(rateLimited),
      reconcileTokenBalance: vi.fn(rateLimited),
    };
    const { bus, mgr, stream, updates } = harness(executor, 10_000);
    bus.emit('openPosition', { mint: MINT, sizeSol: 0.01, highVolatility: false, pricing: pricing() });
    await wait(20);
    stream.ingest(update({ signature: 'BUY', err: { err: 'SlippageExceeded' }, preRaw: '0', postRaw: '0' }));
    await wait(30);
    expect(updates.at(-1)?.state).toBe('FAILED');
    mgr.stop();
  });

  it('adopts a wallet token increase nobody tracks, with exits armed', async () => {
    const executor: Partial<Executor> = {
      buildExitLadder: vi.fn(() => ({ refresh: vi.fn(async () => undefined) }) as never),
    };
    const { mgr, stream, poller, updates } = harness(executor);
    stream.ingest(update({ signature: 'STRAY', postRaw: '24000000000', solPre: '100000000', solPost: '89000000' }));
    await wait(20);
    expect(poller.readPoolPricing).toHaveBeenCalledWith(MINT, 6, true);
    const open = updates.find((u) => u.state === 'OPEN' && u.mint === MINT);
    expect(open?.sizeSol).toBeCloseTo(0.011, 9);
    expect(poller.registered.has(MINT)).toBe(true);
    expect(mgr.entriesHalted).toBeNull(); // adoption skips the price invariant
    mgr.stop();
  });

  it('ignores token increases on a wallet tx for a mint already being entered', async () => {
    let release!: (r: BroadcastResult) => void;
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(() => new Promise<BroadcastResult>((r) => { release = r; })),
      reconcileTokenBalance: vi.fn(async () => 24_000_000_000n),
      buildExitLadder: vi.fn(() => ({ refresh: vi.fn(async () => undefined) }) as never),
    };
    const { bus, mgr, stream, poller } = harness(executor);
    bus.emit('openPosition', { mint: MINT, sizeSol: 0.01, highVolatility: false, pricing: pricing() });
    await wait(5);
    stream.ingest(update({ signature: 'BUY', postRaw: '24000000000' }));
    await wait(10);
    expect(poller.readPoolPricing).not.toHaveBeenCalled();
    release({ ...timedOut('BUY'), confirmed: true, landingUnknown: false });
    await wait(10);
    mgr.stop();
  });

  it('a tx paid by someone else still counts for our token delta', () => {
    const tx = extractWalletTx(update({ signature: 'X', feePayer: OTHER }), WALLET, 0)!;
    expect(tx.feePayer).toBe(OTHER);
    expect(tx.tokenDeltas.get(MINT)?.raw).toBe(5_000_000n);
  });
});
