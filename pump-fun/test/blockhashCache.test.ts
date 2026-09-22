import { describe, expect, it, vi } from 'vitest';
import { BlockhashCache, isBlockhashNotFound } from '../src/executor/blockhashCache.ts';
import type { Connection } from '@solana/web3.js';

/**
 * assembleSignedSwapTx fetched a blockhash on EVERY assemble: one serial round
 * trip inside the entry's pre-send path, and one PER TIER on every
 * ExitLadder.refresh() — 4 serial round trips every 45 s per open position, plus
 * once synchronously while opening a live position.
 */

function fakeConnection(hashes: string[] = ['h1', 'h2', 'h3']) {
  const state = { calls: 0, failNext: false };
  const connection = {
    async getLatestBlockhash() {
      state.calls++;
      if (state.failNext) {
        state.failNext = false;
        throw new Error('rpc unavailable');
      }
      return { blockhash: hashes[Math.min(state.calls - 1, hashes.length - 1)]!, lastValidBlockHeight: 1 };
    },
  } as unknown as Connection;
  return { connection, state };
}

describe('BlockhashCache', () => {
  it('serves one fetch inside the TTL and refetches past it', async () => {
    const clock = { v: 0 };
    const { connection, state } = fakeConnection();
    const cache = new BlockhashCache(connection, 10_000, () => clock.v);

    expect(await cache.get()).toBe('h1');
    clock.v = 9_999;
    expect(await cache.get()).toBe('h1');
    expect(state.calls).toBe(1);

    clock.v = 10_001;
    expect(await cache.get()).toBe('h2');
    expect(state.calls).toBe(2);
  });

  it('coalesces concurrent callers into a single round trip', async () => {
    // This is what makes a 4-tier ladder refresh cost one fetch instead of four.
    const { connection, state } = fakeConnection();
    const cache = new BlockhashCache(connection, 10_000, () => 0);

    const all = await Promise.all([cache.get(), cache.get(), cache.get(), cache.get()]);

    expect(all).toEqual(['h1', 'h1', 'h1', 'h1']);
    expect(state.calls).toBe(1);
  });

  it('refetches after invalidate, so a retry cannot reuse the failed blockhash', async () => {
    const { connection, state } = fakeConnection();
    const cache = new BlockhashCache(connection, 10_000, () => 0);

    expect(await cache.get()).toBe('h1');
    cache.invalidate();
    expect(await cache.get()).toBe('h2');
    expect(state.calls).toBe(2);
  });

  it('serves the stale value when a refresh fails rather than blocking a trade', async () => {
    const clock = { v: 0 };
    const { connection, state } = fakeConnection();
    const cache = new BlockhashCache(connection, 1_000, () => clock.v);

    expect(await cache.get()).toBe('h1');
    clock.v = 2_000;
    state.failNext = true;

    // A blockhash fetch must never be the reason a trade does not go out.
    expect(await cache.get()).toBe('h1');
  });

  it('propagates the error when there is nothing cached to fall back on', async () => {
    const { connection, state } = fakeConnection();
    state.failNext = true;
    const cache = new BlockhashCache(connection, 1_000, () => 0);

    await expect(cache.get()).rejects.toThrow('rpc unavailable');
  });
});

describe('isBlockhashNotFound', () => {
  it('matches the shapes an RPC send actually returns', () => {
    expect(isBlockhashNotFound(new Error('Blockhash not found'))).toBe(true);
    expect(isBlockhashNotFound(new Error('failed to send: BlockhashNotFound'))).toBe(true);
    expect(isBlockhashNotFound(new Error('blockhash   not   found'))).toBe(true);
    expect(isBlockhashNotFound('Transaction simulation failed: Blockhash not found')).toBe(true);
  });

  it('does not match unrelated send failures', () => {
    expect(isBlockhashNotFound(new Error('custom program error: 0x1774'))).toBe(false);
    expect(isBlockhashNotFound(new Error('429 rate limited'))).toBe(false);
    expect(isBlockhashNotFound(undefined)).toBe(false);
  });
});

describe('fee-plan and blockhash caching are configurable off', () => {
  it('a zero TTL means every get is a fresh fetch', async () => {
    const { connection, state } = fakeConnection();
    const cache = new BlockhashCache(connection, 0, () => 0);
    await cache.get();
    await cache.get();
    expect(state.calls).toBe(2);
  });
});

describe('assembleSignedSwapTx blockhash provider', () => {
  it('prefers the provider over a direct getLatestBlockhash', async () => {
    const { assembleSignedSwapTx } = await import('../src/executor/assemble.ts');
    const { Keypair } = await import('@solana/web3.js');
    const kp = Keypair.generate();
    const { connection, state } = fakeConnection();
    const provider = vi.fn(async () => '11111111111111111111111111111111');

    await assembleSignedSwapTx([], {
      connection,
      wallet: {
        keypair: kp,
        assertWhitelisted: () => {},
      } as never,
      feePlan: { priorityMicroLamports: 250_000, jitoTipLamports: 0 },
      blockhashProvider: provider,
    });

    expect(provider).toHaveBeenCalledTimes(1);
    expect(state.calls).toBe(0);
  });

  it('falls back to the connection when no provider is supplied', async () => {
    const { assembleSignedSwapTx } = await import('../src/executor/assemble.ts');
    const { Keypair } = await import('@solana/web3.js');
    const kp = Keypair.generate();
    const { connection, state } = fakeConnection(['11111111111111111111111111111111']);

    await assembleSignedSwapTx([], {
      connection,
      wallet: { keypair: kp, assertWhitelisted: () => {} } as never,
      feePlan: { priorityMicroLamports: 250_000, jitoTipLamports: 0 },
    });

    expect(state.calls).toBe(1);
  });
});
