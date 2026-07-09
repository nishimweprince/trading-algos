import { describe, it, expect } from 'vitest';
import { assertProgramsExist } from '../src/core/programs.ts';
import { ConfigError } from '../src/config/load.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import type { RpcClient } from '../src/core/rpc.ts';

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
});
