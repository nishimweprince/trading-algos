import { describe, it, expect, vi, afterEach } from 'vitest';
import { LaserstreamFeed, type SubscribeFn } from '../src/detector/laserstream.ts';
import { WSOL_MINT, PROGRAM_IDS, PUMP_FUN_MINT_AUTHORITY } from '../src/core/constants.ts';
import { base58Decode } from '../src/core/base58.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import type { FeedGraduation, FeedLaunch } from '../src/core/types.ts';

const TOKEN = '64W4CqYgGzco1Rm5cgHepUWPvqPWWTJ6NRRWwLVGpump';
const AUTH = '39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg';
const flush = () => new Promise((r) => setTimeout(r, 0));

/** Fake SDK: records requests, exposes the data/error callbacks, counts cancels. */
function fakeSdk(opts: { failFirst?: number | ((req: Record<string, unknown>) => boolean) } = {}) {
  const state = {
    requests: [] as Record<string, unknown>[],
    onData: (_: unknown) => {},
    onError: (_: unknown) => {},
    cancelled: 0,
    failures: 0,
  };
  const subscribeFn: SubscribeFn = async (_cfg, request, onData, onError) => {
    state.requests.push(request);
    const shouldFail =
      typeof opts.failFirst === 'function' ? opts.failFirst(request) : state.failures < (opts.failFirst ?? 0);
    if (shouldFail) {
      state.failures++;
      throw new Error(typeof opts.failFirst === 'function' ? 'slot filter not supported on this plan' : 'boom');
    }
    state.onData = onData;
    state.onError = onError ?? (() => {});
    return {
      id: `h${state.requests.length}`,
      cancel: () => {
        state.cancelled++;
      },
      write: async () => {},
    };
  };
  return { state, subscribeFn };
}

function migrateUpdate(signature: string, mints: string[], log = 'Program log: Instruction: MigrateV2', slot: unknown = '4242') {
  return {
    transaction: {
      slot,
      transaction: {
        signature: Buffer.from(signature.padEnd(64, '0')),
        meta: {
          err: null,
          logMessages: [log],
          preTokenBalances: [],
          postTokenBalances: mints.map((mint) => ({ mint })),
        },
      },
    },
  };
}

describe('LaserstreamFeed', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function make(sdk: ReturnType<typeof fakeSdk>, rpcMints: string[] = [TOKEN, WSOL_MINT]) {
    let lookups = 0;
    const rpc = {
      getTransactionTokenMints: async () => {
        lookups++;
        return { mints: rpcMints, slot: 999 };
      },
    } as unknown as RpcClient;
    const grads: FeedGraduation[] = [];
    const health: Array<{ healthy: boolean; detail?: string }> = [];
    const activity: Array<{ kind: string; slot?: number }> = [];
    const feed = new LaserstreamFeed({
      endpoint: 'https://laserstream.example',
      token: 'k',
      rpc,
      pumpFunProgramId: PROGRAM_IDS.PUMP_FUN,
      migrationAuthority: AUTH,
      reconnectBaseMs: 10,
      reconnectMaxMs: 40,
      subscribeFn: sdk.subscribeFn,
    });
    feed.onGraduation((g) => grads.push(g));
    feed.onHealth((healthy, detail) => health.push(detail !== undefined ? { healthy, detail } : { healthy }));
    feed.onActivity((a) => activity.push(a.slot !== undefined ? { kind: a.kind, slot: a.slot } : { kind: a.kind }));
    return { feed, grads, health, activity, lookups: () => lookups };
  }

  it('subscribes with accountRequired narrowing and a real slots heartbeat filter', async () => {
    const sdk = fakeSdk();
    const { feed, health } = make(sdk);
    feed.start();
    await flush();
    const req = sdk.state.requests[0]!;
    const tx = (req['transactions'] as Record<string, Record<string, unknown>>)['pumpfun']!;
    expect(tx['accountRequired']).toEqual([PROGRAM_IDS.PUMP_FUN, AUTH]);
    expect(req['slots']).toEqual({ heartbeat: { filterByCommitment: true } });
    expect(health.at(-1)).toEqual({ healthy: true });
    expect(feed.liveness).toBe('slot');
  });

  it('emits the mint inline from token balances with slot and no RPC lookup', async () => {
    const sdk = fakeSdk();
    const { feed, grads, lookups } = make(sdk);
    feed.start();
    await flush();
    sdk.state.onData(migrateUpdate('SIGA', [TOKEN, WSOL_MINT]));
    expect(grads).toHaveLength(1);
    expect(grads[0]).toMatchObject({ mint: TOKEN, feedSource: 'laserstream', slot: 4242 });
    expect(lookups()).toBe(0);
  });

  it('falls back to the RPC lookup when balances are missing', async () => {
    const sdk = fakeSdk();
    const { feed, grads, lookups } = make(sdk);
    feed.start();
    await flush();
    sdk.state.onData(migrateUpdate('SIGB', []));
    await vi.waitFor(() => expect(grads).toHaveLength(1));
    expect(lookups()).toBe(1);
    expect(grads[0]!.slot).toBe(4242);
  });

  it('ignores legacy-anchored non-migrations and reports slot updates as activity', async () => {
    const sdk = fakeSdk();
    const { feed, grads, activity } = make(sdk);
    feed.start();
    await flush();
    sdk.state.onData(migrateUpdate('SIGC', [TOKEN], 'Program log: Instruction: MigrateBondingCurveCreator'));
    expect(grads).toHaveLength(0);
    sdk.state.onData({ slot: { slot: '123', parent: '122', status: 0 } });
    expect(activity.at(-1)).toEqual({ kind: 'slot', slot: 123 });
  });

  it('retries a failed initial subscribe with backoff', async () => {
    vi.useFakeTimers();
    const sdk = fakeSdk({ failFirst: 2 });
    const { feed, health } = make(sdk);
    feed.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(sdk.state.requests).toHaveLength(1);
    expect(health.at(-1)).toEqual({ healthy: false, detail: 'subscribe failed' });
    await vi.advanceTimersByTimeAsync(10);
    expect(sdk.state.requests).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(20);
    expect(sdk.state.requests).toHaveLength(3);
    expect(health.at(-1)).toEqual({ healthy: true });
  });

  it('drops the slots filter and degrades liveness when the server rejects it', async () => {
    vi.useFakeTimers();
    const sdk = fakeSdk({ failFirst: (req) => Object.keys(req['slots'] as object).length > 0 });
    const { feed } = make(sdk);
    feed.start();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(10);
    expect(sdk.state.requests).toHaveLength(2);
    expect(sdk.state.requests[1]!['slots']).toEqual({});
    expect(feed.liveness).toBe('silence');
  });

  it('reconnect() cancels the handle, resubscribes, and ignores frames from the old handle', async () => {
    vi.useFakeTimers();
    const sdk = fakeSdk();
    const { feed, grads, health } = make(sdk);
    feed.start();
    await vi.advanceTimersByTimeAsync(0);
    const oldOnData = sdk.state.onData;
    feed.reconnect('watchdog');
    expect(sdk.state.cancelled).toBe(1);
    expect(health.at(-1)).toEqual({ healthy: false, detail: 'watchdog' });
    feed.reconnect('again'); // pending → no-op
    expect(sdk.state.cancelled).toBe(1);
    await vi.advanceTimersByTimeAsync(10);
    expect(sdk.state.requests).toHaveLength(2);
    oldOnData(migrateUpdate('OLD', [TOKEN]));
    expect(grads).toHaveLength(0);
    sdk.state.onData(migrateUpdate('NEW', [TOKEN]));
    expect(grads).toHaveLength(1);
  });

  describe('launch (create) feed', () => {
    const CREATOR = '37NfU4o2v7nPG3azf47n3C5ahYW5sPZXPUj2WFdpdBov';
    const OLD = 'Ed6nr6RJJz24Kkq1ZKw9kfc8Ri8bHgBLkX93WFD7fwGC';
    const bal = (mint: string, owner: string, amount: string) => ({ mint, owner, uiTokenAmount: { amount } });

    function createUpdate(opts: { newMints: string[]; preMints?: string[]; log?: string }) {
      return {
        transaction: {
          slot: '777',
          transaction: {
            signature: Buffer.from('CREATESIG'.padEnd(64, '0')),
            transaction: { message: { accountKeys: [Buffer.from(base58Decode(CREATOR))] } },
            meta: {
              err: null,
              logMessages: [opts.log ?? 'Program log: Instruction: CreateV2'],
              preTokenBalances: (opts.preMints ?? []).map((m) => bal(m, 'Curve', '5')),
              postTokenBalances: [
                ...opts.newMints.map((m) => bal(m, 'Curve', '1000000000000000')),
                ...(opts.preMints ?? []).map((m) => bal(m, 'Curve', '4')),
              ],
            },
          },
        },
      };
    }

    function makeLaunches(sdk: ReturnType<typeof fakeSdk>, now = () => 1_000) {
      const launches: FeedLaunch[] = [];
      const feed = new LaserstreamFeed({
        endpoint: 'https://laserstream.example',
        token: 'k',
        rpc: {} as RpcClient,
        pumpFunProgramId: PROGRAM_IDS.PUMP_FUN,
        migrationAuthority: AUTH,
        launchesEnabled: true,
        subscribeFn: sdk.subscribeFn,
        now,
      });
      feed.onLaunch((l) => launches.push(l));
      return { feed, launches };
    }

    it('adds a creation-only filter narrowed on the pump.fun mint authority', async () => {
      const sdk = fakeSdk();
      const { feed } = makeLaunches(sdk);
      feed.start();
      await flush();
      const tx = (sdk.state.requests[0]!['transactions'] as Record<string, Record<string, unknown>>)['pumpfunCreate']!;
      expect(tx['accountRequired']).toEqual([PROGRAM_IDS.PUMP_FUN, PUMP_FUN_MINT_AUTHORITY]);
    });

    it('emits the created mint (post balance, no pre balance) with its slot and fee payer', async () => {
      const sdk = fakeSdk();
      const { feed, launches } = makeLaunches(sdk);
      feed.start();
      await flush();
      sdk.state.onData(createUpdate({ newMints: [TOKEN], preMints: [OLD] })); // bundled buy of an existing token
      expect(launches).toHaveLength(1);
      expect(launches[0]).toMatchObject({ mint: TOKEN, feedSource: 'laserstream', slot: 777, creator: CREATOR });
    });

    it('records every mint of a multi-create tx (coverage treats an unseen create as old)', async () => {
      const sdk = fakeSdk();
      const { feed, launches } = makeLaunches(sdk);
      feed.start();
      await flush();
      sdk.state.onData(createUpdate({ newMints: [TOKEN, OLD] }));
      expect(launches.map((l) => l.mint).sort()).toEqual([OLD, TOKEN].sort());
    });

    it('ignores wrapper and pool-creation logs', async () => {
      const sdk = fakeSdk();
      const { feed, launches } = makeLaunches(sdk);
      feed.start();
      await flush();
      sdk.state.onData(createUpdate({ newMints: [TOKEN], log: 'Program log: Instruction: CreateCoinAndBuyBondingCurveV3' }));
      sdk.state.onData(createUpdate({ newMints: [TOKEN], log: 'Program log: Instruction: CreatePool' }));
      expect(launches).toHaveLength(0);
    });

    it('claims coverage from the subscribe, and restarts the window after a stream error', async () => {
      let now = 1_000;
      const sdk = fakeSdk();
      const { feed } = makeLaunches(sdk, () => now);
      expect(feed.launchCoverageSinceMs).toBeNull();
      feed.start();
      await flush();
      expect(feed.launchCoverageSinceMs).toBe(1_000);
      now = 5_000;
      sdk.state.onError(new Error('blip'));
      expect(feed.launchCoverageSinceMs).toBeNull();
      now = 6_000;
      sdk.state.onData({ slot: { slot: '1' } });
      expect(feed.launchCoverageSinceMs).toBe(6_000);
    });

    it('claims no coverage when launches are off', async () => {
      const sdk = fakeSdk();
      const { feed } = make(sdk);
      feed.start();
      await flush();
      expect(feed.launchCoverageSinceMs).toBeNull();
    });
  });

  it('carries the migrate tx post balances (pool vaults) on the graduation', async () => {
    const sdk = fakeSdk();
    const { feed, grads } = make(sdk);
    feed.start();
    await flush();
    const u = migrateUpdate('SIGBAL', [TOKEN, WSOL_MINT]);
    (u.transaction.transaction.meta.postTokenBalances as unknown[]) = [
      { mint: TOKEN, owner: 'Pool', uiTokenAmount: { amount: '206900000000000' } },
      { mint: WSOL_MINT, owner: 'Pool', uiTokenAmount: { amount: '84000000000' } },
    ];
    sdk.state.onData(u);
    expect(grads[0]!.txBalances).toEqual([
      { mint: TOKEN, owner: 'Pool', amount: 206_900_000_000_000n },
      { mint: WSOL_MINT, owner: 'Pool', amount: 84_000_000_000n },
    ]);
  });
});
