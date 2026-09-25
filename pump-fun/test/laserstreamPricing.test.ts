import { describe, expect, it } from 'vitest';
import { LaserstreamPriceIngest, extractAccountUpdate } from '../src/positions/laserstreamPricing.ts';
import type { PriceTick } from '../src/positions/pricing.ts';
import { base58Decode } from '../src/core/base58.ts';

const SOL = 10n ** 9n;
const BASE = 10n ** 15n;
const QUOTE = 100n * SOL;
const KEY = 'So11111111111111111111111111111111111111112';

function tokenAccount(amount: bigint): Uint8Array {
  const buf = Buffer.alloc(165);
  buf.writeBigUInt64LE(amount, 64);
  return Uint8Array.from(buf);
}

/** Fake SDK: records requests, exposes the data callback. */
function fakeSdk() {
  const state = { requests: [] as unknown[], writes: [] as unknown[], onData: (_: unknown) => {}, cancelled: 0 };
  const subscribeFn = async (
    _cfg: unknown,
    request: Record<string, unknown>,
    onData: (u: unknown) => void | Promise<void>,
  ) => {
    state.requests.push(request);
    state.onData = onData;
    return {
      id: 'h',
      cancel: () => {
        state.cancelled++;
      },
      write: async (r: unknown) => {
        state.writes.push(r);
      },
    };
  };
  return { state, subscribeFn };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

describe('extractAccountUpdate', () => {
  it('decodes pubkey + token amount from an account update', () => {
    const u = extractAccountUpdate({ account: { account: { pubkey: base58Decode(KEY), data: tokenAccount(42n) }, slot: 1 } });
    expect(u).toEqual({ address: KEY, amount: 42n });
  });
  it('ignores pings, slots and non-token accounts', () => {
    expect(extractAccountUpdate({ ping: {} })).toBeNull();
    expect(extractAccountUpdate({ slot: { slot: 1 } })).toBeNull();
    expect(extractAccountUpdate({ account: { account: { pubkey: base58Decode(KEY), data: new Uint8Array(10) } } })).toBeNull();
  });
});

describe('LaserstreamPriceIngest', () => {
  it('stays inert without an endpoint', () => {
    const ingest = new LaserstreamPriceIngest({ endpoint: '', minIntervalMs: 0 });
    ingest.start();
    const ticks: PriceTick[] = [];
    ingest.register({ mint: 'M', baseVault: 'b', quoteVault: 'q', baseDecimals: 6 }, (t) => ticks.push(t));
    expect(ingest.applyAccountUpdate('b', BASE)).toHaveLength(0); // no quote yet
    expect(ingest.stats.healthy).toBe(false);
  });

  it('subscribes to every tracked account and rewrites the request as the set changes', async () => {
    const { state, subscribeFn } = fakeSdk();
    const ingest = new LaserstreamPriceIngest({ endpoint: 'https://ls', token: 't', subscribeFn, minIntervalMs: 0 });
    ingest.start();
    await flush();
    expect(state.requests).toHaveLength(1);
    ingest.register({ mint: 'M', baseVault: 'b', quoteVault: 'q', baseDecimals: 6, creatorAta: 'c' }, () => {});
    await flush();
    const last = state.writes.at(-1) as { accounts: { vaults: { account: string[] } } };
    expect(last.accounts.vaults.account.sort()).toEqual(['b', 'c', 'q']);
    ingest.unregister('M');
    await flush();
    const after = state.writes.at(-1) as { accounts: Record<string, unknown> };
    expect(after.accounts).toEqual({});
    await ingest.stop();
    expect(state.cancelled).toBe(1);
  });

  it('seeds from the entry reserves so the first vault update already yields a tick', () => {
    const ingest = new LaserstreamPriceIngest({ endpoint: 'https://ls', minIntervalMs: 0, subscribeFn: fakeSdk().subscribeFn });
    const ticks: PriceTick[] = [];
    ingest.register(
      { mint: 'M', baseVault: 'b', quoteVault: 'q', baseDecimals: 6, creatorAta: 'c' },
      (t) => ticks.push(t),
      { baseReserve: BASE, quoteReserveLamports: QUOTE },
    );
    const out = ingest.applyAccountUpdate('q', 120n * SOL, 1000);
    expect(out).toHaveLength(1);
    expect(ticks[0]!.price).toBeCloseTo(120 / 1e9, 18);
    expect(ticks[0]!.quoteReserveLamports).toBe(120n * SOL);
    expect(ticks[0]!.creatorBaseBalance).toBeUndefined();
    ingest.applyAccountUpdate('c', 5n, 1001);
    expect(ticks[1]!.creatorBaseBalance).toBe(5n);
  });

  it('routes SDK updates through the same path', async () => {
    const { state, subscribeFn } = fakeSdk();
    const ingest = new LaserstreamPriceIngest({ endpoint: 'https://ls', subscribeFn, minIntervalMs: 0 });
    ingest.start();
    await flush();
    const ticks: PriceTick[] = [];
    ingest.register({ mint: 'M', baseVault: KEY, quoteVault: 'q', baseDecimals: 6 }, (t) => ticks.push(t), {
      baseReserve: BASE,
      quoteReserveLamports: QUOTE,
    });
    state.onData({ account: { account: { pubkey: base58Decode(KEY), data: tokenAccount(BASE / 2n) } } });
    expect(ticks).toHaveLength(1);
    expect(ticks[0]!.baseReserve).toBe(BASE / 2n);
    expect(ticks[0]!.price).toBeCloseTo(200 / 1e9, 18);
    expect(ingest.stats.updates).toBe(1);
  });

  it('coalesces a burst to one tick per interval, keeping the trailing state', async () => {
    let clock = 0;
    const ingest = new LaserstreamPriceIngest({
      endpoint: 'https://ls',
      minIntervalMs: 50,
      now: () => clock,
      subscribeFn: fakeSdk().subscribeFn,
    });
    const ticks: PriceTick[] = [];
    ingest.register({ mint: 'M', baseVault: 'b', quoteVault: 'q', baseDecimals: 6 }, (t) => ticks.push(t), {
      baseReserve: BASE,
      quoteReserveLamports: QUOTE,
    });
    clock = 100;
    expect(ingest.applyAccountUpdate('q', 101n * SOL)).toHaveLength(1);
    clock = 110;
    expect(ingest.applyAccountUpdate('q', 102n * SOL)).toHaveLength(0); // within 50 ms → deferred
    clock = 120;
    expect(ingest.applyAccountUpdate('q', 103n * SOL)).toHaveLength(0);
    await new Promise((r) => setTimeout(r, 60));
    clock = 200;
    expect(ticks).toHaveLength(2);
    expect(ticks[1]!.quoteReserveLamports).toBe(103n * SOL); // last state of the burst
    await ingest.stop();
  });

  it('forgets balances of accounts no pool reads any more', () => {
    const ingest = new LaserstreamPriceIngest({ endpoint: 'https://ls', minIntervalMs: 0, subscribeFn: fakeSdk().subscribeFn });
    ingest.register({ mint: 'M', baseVault: 'b', quoteVault: 'q', baseDecimals: 6 }, () => {}, {
      baseReserve: BASE,
      quoteReserveLamports: QUOTE,
    });
    ingest.unregister('M');
    expect(ingest.stats.accounts).toBe(0);
    expect(ingest.applyAccountUpdate('q', 1n)).toHaveLength(0);
  });
});

/**
 * Liveness. The SDK owns its own reconnect and re-subscribes with the request it
 * was originally handed, which can come back without our current account filter.
 * `resubscribe()` then short-circuits on key equality, so the stream stays
 * subscribed to the wrong set and delivers nothing — at warn level, forever, with
 * `stats.healthy` having had no consumer anywhere in the codebase.
 */
function fakeSdkWithError() {
  const state = {
    requests: [] as unknown[],
    writes: [] as unknown[],
    onData: (_: unknown) => {},
    onError: (_: unknown) => {},
    cancelled: 0,
    subscribes: 0,
  };
  const subscribeFn = async (
    _cfg: unknown,
    request: Record<string, unknown>,
    onData: (u: unknown) => void | Promise<void>,
    onError?: (e: unknown) => void,
  ) => {
    state.subscribes++;
    state.requests.push(request);
    state.onData = onData;
    if (onError) state.onError = onError;
    return {
      id: 'h',
      cancel: () => { state.cancelled++; },
      write: async (r: unknown) => { state.writes.push(r); },
    };
  };
  return { state, subscribeFn };
}

describe('LaserstreamPriceIngest liveness', () => {
  it('re-pushes the account filter after a stream error', async () => {
    const { state, subscribeFn } = fakeSdkWithError();
    const ingest = new LaserstreamPriceIngest({ endpoint: 'https://ls', subscribeFn, minIntervalMs: 0 });
    ingest.start();
    await flush();
    ingest.register({ mint: 'M', baseVault: 'b', quoteVault: 'q', baseDecimals: 6 }, () => {});
    await flush();
    const writesBefore = state.writes.length;

    state.onError(new Error('stream reset'));
    await flush();

    expect(ingest.stats.healthy).toBe(false);
    // Pre-fix this was 0: lastWrittenKey still matched, so resubscribe() bailed.
    expect(state.writes.length).toBeGreaterThan(writesBefore);
    await ingest.stop();
  });

  it('tears down and reconnects when tracking pools but delivering no ticks', async () => {
    const clock = { v: 0 };
    const { state, subscribeFn } = fakeSdkWithError();
    const ingest = new LaserstreamPriceIngest({
      endpoint: 'https://ls',
      subscribeFn,
      minIntervalMs: 0,
      now: () => clock.v,
    });
    ingest.start();
    await flush();
    ingest.register({ mint: 'M', baseVault: 'b', quoteVault: 'q', baseDecimals: 6 }, () => {});
    await flush();
    expect(state.subscribes).toBe(1);

    clock.v = 10_000;
    expect(ingest.reconnectIfStale(30_000)).toBe(false);

    clock.v = 31_000;
    expect(ingest.reconnectIfStale(30_000)).toBe(true);
    await flush();

    expect(state.cancelled).toBe(1);
    expect(state.subscribes).toBe(2);
    await ingest.stop();
  });

  it('does not reconnect a stream that is delivering ticks', async () => {
    const clock = { v: 0 };
    const { state, subscribeFn } = fakeSdkWithError();
    const ingest = new LaserstreamPriceIngest({
      endpoint: 'https://ls',
      subscribeFn,
      minIntervalMs: 0,
      now: () => clock.v,
    });
    ingest.start();
    await flush();
    ingest.register({ mint: 'M', baseVault: 'b', quoteVault: 'q', baseDecimals: 6 }, () => {}, {
      baseReserve: BASE,
      quoteReserveLamports: QUOTE,
    });
    await flush();

    clock.v = 29_000;
    expect(ingest.applyAccountUpdate('q', QUOTE * 2n)).toHaveLength(1);
    expect(ingest.stats.lastTickAtMs).toBe(29_000);

    clock.v = 50_000; // stale vs subscribe time, fresh vs the last tick
    expect(ingest.reconnectIfStale(30_000)).toBe(false);
    expect(state.subscribes).toBe(1);
    await ingest.stop();
  });

  it('never reconnects while nothing is tracked', async () => {
    const clock = { v: 0 };
    const { state, subscribeFn } = fakeSdkWithError();
    const ingest = new LaserstreamPriceIngest({ endpoint: 'https://ls', subscribeFn, minIntervalMs: 0, now: () => clock.v });
    ingest.start();
    await flush();

    clock.v = 600_000;
    expect(ingest.reconnectIfStale(30_000)).toBe(false);
    expect(state.subscribes).toBe(1);
    await ingest.stop();
  });
});
