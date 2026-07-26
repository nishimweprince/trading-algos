import { describe, it, expect, vi } from 'vitest';
import { assertProgramsExist } from '../src/core/programs.ts';
import { ConfigError } from '../src/config/load.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { RpcError, type RpcClient } from '../src/core/rpc.ts';

const config = ConfigSchema.parse({ mode: 'paper' });

function fakeRpc(byId: Record<string, { executable: boolean } | null>): RpcClient {
  return {
    getMultipleAccountsBase64: async (ids: string[]) =>
      ids.map((id) => {
        const v = byId[id];
        return v ? { data: '', owner: '', lamports: 1, executable: v.executable } : null;
      }),
  } as unknown as RpcClient;
}

const ALL_OK = {
  '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P': { executable: true },
  pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA: { executable: true },
  pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ: { executable: true },
  '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8': { executable: true },
};

describe('assertProgramsExist', () => {
  it('passes when all programs exist and are executable', async () => {
    await expect(assertProgramsExist(fakeRpc(ALL_OK), config)).resolves.toBeUndefined();
  });

  it('throws when a program is missing', async () => {
    const missing = { ...ALL_OK, pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA: null };
    await expect(assertProgramsExist(fakeRpc(missing), config)).rejects.toBeInstanceOf(ConfigError);
  });

  it('throws when a program account is not executable', async () => {
    const notExec = { ...ALL_OK, '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P': { executable: false } };
    await expect(assertProgramsExist(fakeRpc(notExec), config)).rejects.toBeInstanceOf(ConfigError);
  });

  /**
   * A 429 says nothing about program IDs. Treating "couldn't verify" as
   * "verification failed" took the whole bot down AND made the rate limit
   * worse — the supervisor restarted it immediately and each restart spent
   * more of the exhausted quota.
   */
  it('boots anyway when the RPC never answers, instead of crash-looping', async () => {
    vi.useFakeTimers();
    try {
      const rpc = {
        getMultipleAccountsBase64: vi.fn(async () => {
          throw new RpcError('getMultipleAccounts HTTP 429', true);
        }),
      } as unknown as RpcClient;

      const promise = assertProgramsExist(rpc, config);
      await vi.runAllTimersAsync();
      await expect(promise).resolves.toBeUndefined();
      // Retried with real backoff rather than giving up after ~360ms.
      expect((rpc.getMultipleAccountsBase64 as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('recovers when a transient failure clears mid-retry', async () => {
    vi.useFakeTimers();
    try {
      let calls = 0;
      const rpc = {
        getMultipleAccountsBase64: async (ids: string[]) => {
          if (++calls === 1) throw new RpcError('getMultipleAccounts HTTP 429', true);
          return ids.map((id) => {
            const v = (ALL_OK as Record<string, { executable: boolean }>)[id];
            return v ? { data: '', owner: '', lamports: 1, executable: v.executable } : null;
          });
        },
      } as unknown as RpcClient;

      const promise = assertProgramsExist(rpc, config);
      await vi.runAllTimersAsync();
      await expect(promise).resolves.toBeUndefined();
      expect(calls).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('still refuses to start when the RPC answers and a program is wrong', async () => {
    // Non-retryable errors and conclusive failures must never be swallowed.
    const missing = { ...ALL_OK, pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA: null };
    await expect(assertProgramsExist(fakeRpc(missing), config)).rejects.toBeInstanceOf(ConfigError);

    const hardFail = {
      getMultipleAccountsBase64: async () => {
        throw new RpcError('getMultipleAccounts HTTP 401', false);
      },
    } as unknown as RpcClient;
    await expect(assertProgramsExist(hardFail, config)).rejects.toBeInstanceOf(RpcError);
  });
});
