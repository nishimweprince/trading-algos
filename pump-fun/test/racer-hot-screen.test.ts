import { readFileSync } from 'node:fs';
import { describe, expect, it, vi } from 'vitest';
import { Keypair } from '@solana/web3.js';
import { PUMP_AMM_SDK } from '@pump-fun/pump-swap-sdk';
import { HotScreen } from '../src/racer/hotScreen.ts';
import { screenRacerTrack, SCREENING_ENGINE_HOT } from '../src/racer/screening.ts';
import { SendReadyProbe } from '../src/racer/sendReady.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { toAccountInfo, type RawAccountRead } from '../src/executor/swapState.ts';
import { Wallet } from '../src/executor/wallet.ts';
import { deriveAta } from '../src/core/ata.ts';
import { PROGRAM_IDS, WSOL_MINT } from '../src/core/constants.ts';
import { randomSenderTipAccount } from '../src/executor/heliusSender.ts';

const MINT = 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA';
const CREATOR = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb';
const config = ConfigSchema.parse({ mode: 'dry-run' });

function fresh() {
  const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
  return { repos, hot: new HotScreen(config, repos) };
}
const input = (over: Partial<Parameters<HotScreen['screen']>[0]> = {}) => ({
  mint: MINT,
  poolAddress: 'Pool1111111111111111111111111111111111111111',
  migrateSlot: 100,
  vaultQuoteLamports: 67_400_000_000n,
  virtualQuoteLamports: 17_584_505_290n,
  ...over,
});

describe('racer hot-path screen', () => {
  it('returns the same verdict as the post-hoc screenRacerTrack for identical inputs (parity)', () => {
    for (const transferFee of [null, { bps: 300, maxRaw: '1000' }] as const) {
      const { repos, hot } = fresh();
      hot.prepare(MINT, { creator: CREATOR, isToken2022: true, transferFee });
      const r = hot.screen(input());
      const post = screenRacerTrack(
        { mint: MINT, poolAddress: input().poolAddress, migrateSlot: 100, reachQuoteSol: (67.4e9 + 17_584_505_290) / 1e9, quoteVaultSol: 67.4, coinCreator: CREATOR, isToken2022: true, transferFee },
        config,
        repos,
      )!;
      expect(r.prepared).toBe(true);
      expect(r.verdict!.verdict).toBe(post.verdict);
      expect(r.verdict!.fails).toEqual(post.fails);
      expect(r.verdict!.engine).toBe(SCREENING_ENGINE_HOT);
    }
  });

  it('a T22 transfer fee vetoes; a fee-free T22 mint read at admission does not fail closed', () => {
    const { hot } = fresh();
    hot.prepare(MINT, { creator: CREATOR, isToken2022: true, transferFee: { bps: 100, maxRaw: '5' } });
    expect(hot.screen(input()).verdict!.fails[0]).toMatchObject({ id: 'P0', reason: 'rug_extension' });
    hot.prepare(MINT, { creator: CREATOR, isToken2022: true, transferFee: null });
    expect(hot.screen(input()).verdict!.fails.find((f) => f.reason === 't22_fee_unreadable')).toBeUndefined();
  });

  it('makes no DB call on the hot path for a prepared mint', () => {
    const { repos, hot } = fresh();
    hot.prepare(MINT, { creator: CREATOR, isToken2022: false, transferFee: null });
    const spies = [vi.spyOn(repos, 'isMintBlacklisted'), vi.spyOn(repos, 'isCreatorBlacklisted'), vi.spyOn(repos, 'launchByMint')];
    hot.screen(input());
    for (const s of spies) expect(s).not.toHaveBeenCalled();
    expect(hot.stats.dbFallbacks).toBe(0);
  });

  it('serves blacklist answers precomputed at admission (and refreshed at completion)', () => {
    const { repos, hot } = fresh();
    hot.prepare(MINT, { creator: CREATOR, isToken2022: false, transferFee: null });
    repos.blacklistCreator(CREATOR, 'test');
    // Not yet refreshed: the precomputed answer stands.
    const before = hot.screen(input()).verdict!.fails.some((f) => f.id === 'H8');
    hot.refresh(MINT);
    const after = hot.screen(input()).verdict!.fails.some((f) => f.id === 'H8');
    expect(before).toBe(false);
    expect(after).toBe(true);
  });

  it('an unprepared mint falls back to the DB (counted) and an unverified T22 fails closed', () => {
    const { hot } = fresh();
    const r = hot.screen(input({ isToken2022Fallback: true }));
    expect(r.prepared).toBe(false);
    expect(r.accept).toBe(false);
    expect(r.verdict!.fails[0]).toMatchObject({ id: 'P0', reason: 't22_fee_unreadable' });
    expect(hot.stats.unprepared).toBe(1);
    expect(hot.stats.dbFallbacks).toBeGreaterThan(0);
  });
});

describe('send-ready probe with the hot screen', () => {
  const fx = JSON.parse(readFileSync(new URL('./fixtures/pumpswap-buy-state.json', import.meta.url), 'utf8')) as {
    mint: string; pool: string; accounts: Record<string, RawAccountRead>;
  };
  const [g, f] = Object.keys(fx.accounts).slice(2) as [string, string];
  const configs = {
    globalConfig: PUMP_AMM_SDK.decodeGlobalConfig(toAccountInfo(fx.accounts[g]!)),
    feeConfig: PUMP_AMM_SDK.decodeFeeConfig(toAccountInfo(fx.accounts[f]!)),
  };
  function probe() {
    const wallet = Wallet.load('RACER_HOT_SCREEN_TEST_NO_KEY', 'dry-run');
    const p = new SendReadyProbe({
      configs: () => configs,
      blockhash: async () => Keypair.generate().publicKey.toBase58(),
      wallet,
      feePlan: { priorityMicroLamports: 1, jitoTipLamports: 0, exitTipLamports: 0 },
      tipAccount: () => randomSenderTipAccount(),
      quoteLamports: 100_000_000n,
      slippagePct: 5,
    });
    void p.prepare(fx.mint, {
      poolKey: fx.pool,
      baseMint: fx.accounts[fx.mint]!,
      userBaseAta: deriveAta(wallet.publicKey, fx.mint, fx.accounts[fx.mint]!.owner === PROGRAM_IDS.TOKEN_2022),
      userQuoteAta: deriveAta(wallet.publicKey, WSOL_MINT, false),
    });
    p.onPool(fx.mint, fx.accounts[fx.pool]!);
    return p;
  }
  const base = { mint: '', receivedAtNs: 0n, baseReserve: 206_900_000_000_000n, quoteReserveLamports: 67_400_000_000n };

  it('a veto stops the send path before any build, with the screen timed', async () => {
    const p = probe();
    let sawV = -1n;
    const r = await p.probe({ ...base, mint: fx.mint, receivedAtNs: process.hrtime.bigint(), screen: ({ virtualQuoteLamports }) => ((sawV = virtualQuoteLamports), false) });
    expect(r).toMatchObject({ ok: false, reason: 'vetoed' });
    expect(r.ok ? null : r.screenUs).toBeGreaterThanOrEqual(0);
    expect(sawV).toBe(BigInt(PUMP_AMM_SDK.decodePool(toAccountInfo(fx.accounts[fx.pool]!)).virtualQuoteReserves.toString()));
  });

  it('an accept builds and signs; send-ready includes the screen', async () => {
    const p = probe();
    const r = await p.probe({ ...base, mint: fx.mint, receivedAtNs: process.hrtime.bigint(), screen: () => true });
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.screenUs).not.toBeNull();
      expect(r.totalUs).toBeGreaterThanOrEqual((r.screenUs ?? 0) + r.buildUs + r.signUs);
    }
  });
});

describe('dashboard: hot-path screen stats', () => {
  it('reports screen latency, accept rate and precomputed-input rate', async () => {
    const { getRacerSummary } = await import('../src/dashboard/queries.ts');
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    for (const [kind, v] of [
      ['racer_send_screen_us', 12], ['racer_send_screen_us', 30],
      ['racer_screen_accept', 1], ['racer_screen_accept', 0],
      ['racer_screen_prepared', 1], ['racer_screen_prepared', 1],
    ] as const) repos.recordLatencySample({ kind, latencyMs: v, mint: 'M' });
    const s = getRacerSummary(db).latency.sending;
    expect(s).toMatchObject({ screenDecisions: 2, screenAcceptRate: 0.5, screenPreparedRate: 1 });
    expect(s.stagesMs.screen.p50).toBeCloseTo(0.021, 6);
  });
});
