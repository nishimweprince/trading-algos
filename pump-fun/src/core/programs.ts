import { RpcError, type RpcClient } from './rpc.ts';
import type { Config } from '../config/schema.ts';
import { PROGRAM_IDS } from './constants.ts';
import { ConfigError } from '../config/load.ts';
import { logger } from './logger.ts';

const log = logger.child({ mod: 'programs' });

/** Boot-time retry budget. Generous on purpose — see assertProgramsExist. */
const BOOT_ATTEMPTS = 5;
const BOOT_BACKOFF_MS = [1_000, 3_000, 8_000, 20_000];

const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Startup on-chain assertion of the program IDs the bot depends on (Section 4.1).
 * pump.fun / PumpSwap interfaces change; a wrong or unmigrated program ID would
 * silently break detection or execution. One `getMultipleAccounts` verifies each
 * effective ID (config override else pinned default) exists AND is executable.
 *
 * Two outcomes are deliberately NOT the same thing:
 *
 *   - CONCLUSIVE failure — the RPC answered and a program is missing or is not
 *     executable. That is a real config error: throw, refuse to start blind.
 *   - INCONCLUSIVE — the RPC never answered (429 / 5xx / timeout / network). A
 *     rate-limit response carries zero information about program IDs, so
 *     refusing to boot buys no safety. Worse, it is self-reinforcing: the
 *     process exits, the supervisor restarts it immediately, and every restart
 *     spends more of the quota that caused the failure. Warn loudly and
 *     continue; the pinned IDs are still the defaults that shipped.
 *
 * The retry budget here is far longer than RpcClient's own (which is tuned for
 * the trading hot path, ~360 ms total). This is a one-off startup check with no
 * latency requirement, so it can afford to wait out a rate-limit window.
 */
export async function assertProgramsExist(rpc: RpcClient, config: Config): Promise<void> {
  const targets: Array<{ name: string; id: string }> = [
    { name: 'pumpFun', id: config.programs.pumpFun ?? PROGRAM_IDS.PUMP_FUN },
    { name: 'pumpSwap', id: config.programs.pumpSwap ?? PROGRAM_IDS.PUMP_SWAP },
    { name: 'pumpFee', id: PROGRAM_IDS.PUMP_FEE },
    { name: 'raydiumAmm', id: config.programs.raydiumAmm ?? PROGRAM_IDS.RAYDIUM_AMM },
  ];
  const ids = targets.map((t) => t.id);

  let lastErr: unknown;
  for (let attempt = 0; attempt < BOOT_ATTEMPTS; attempt++) {
    try {
      const accts = await rpc.getMultipleAccountsBase64(ids);
      const failures: string[] = [];
      targets.forEach((t, i) => {
        const a = accts[i];
        if (!a) failures.push(`${t.name} (${t.id}): not found on-chain`);
        else if (!a.executable) failures.push(`${t.name} (${t.id}): account is not an executable program`);
      });

      // Conclusive: the chain answered and disagrees with our pinned IDs.
      if (failures.length > 0) {
        throw new ConfigError(
          `program-ID on-chain assertion failed:\n${failures.map((f) => `  - ${f}`).join('\n')}`,
        );
      }
      return;
    } catch (err) {
      if (err instanceof ConfigError) throw err; // conclusive — never retry or swallow
      if (!(err instanceof RpcError) || !err.retryable) throw err;

      lastErr = err;
      const backoff = BOOT_BACKOFF_MS[attempt];
      if (backoff === undefined) break; // budget exhausted
      log.warn('program-ID assertion could not reach the RPC — retrying', {
        attempt: attempt + 1,
        of: BOOT_ATTEMPTS,
        retryInMs: backoff,
        err: (err as Error).message,
      });
      await delay(backoff);
    }
  }

  // Inconclusive after the full budget. Boot anyway rather than crash-loop.
  log.error(
    'program-ID on-chain assertion INCONCLUSIVE — the RPC never answered. ' +
      'Booting with the pinned/configured program IDs UNVERIFIED. This is usually a rate-limited ' +
      'or unreachable RPC endpoint, not a bad program ID. Verify the endpoint and its quota.',
    { err: (lastErr as Error | undefined)?.message, ids },
  );
}
