import type { RpcClient } from './rpc.ts';
import type { Config } from '../config/schema.ts';
import { PROGRAM_IDS } from './constants.ts';
import { ConfigError } from '../config/load.ts';

/**
 * Startup on-chain assertion of the program IDs the bot depends on (Section 4.1).
 * pump.fun / PumpSwap interfaces change; a wrong or unmigrated program ID would
 * silently break detection or execution. One `getMultipleAccounts` verifies each
 * effective ID (config override else pinned default) exists AND is executable.
 * Throws ConfigError naming every failure, so the bot refuses to start blind.
 */
export async function assertProgramsExist(rpc: RpcClient, config: Config): Promise<void> {
  const targets: Array<{ name: string; id: string }> = [
    { name: 'pumpFun', id: config.programs.pumpFun ?? PROGRAM_IDS.PUMP_FUN },
    { name: 'pumpSwap', id: config.programs.pumpSwap ?? PROGRAM_IDS.PUMP_SWAP },
    { name: 'pumpFee', id: PROGRAM_IDS.PUMP_FEE },
    { name: 'raydiumAmm', id: config.programs.raydiumAmm ?? PROGRAM_IDS.RAYDIUM_AMM },
  ];

  const accts = await rpc.getMultipleAccountsBase64(targets.map((t) => t.id));
  const failures: string[] = [];
  targets.forEach((t, i) => {
    const a = accts[i];
    if (!a) failures.push(`${t.name} (${t.id}): not found on-chain`);
    else if (!a.executable) failures.push(`${t.name} (${t.id}): account is not an executable program`);
  });

  if (failures.length > 0) {
    throw new ConfigError(`program-ID on-chain assertion failed:\n${failures.map((f) => `  - ${f}`).join('\n')}`);
  }
}
