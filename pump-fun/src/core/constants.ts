import type { Address } from './types.ts';

/**
 * Well-known program IDs and addresses (Section 4.1).
 *
 * IMPORTANT: pump.fun / PumpSwap program interfaces change (Section 13). These
 * are pinned here as the defaults but are overridable via config.programs.*.
 * A startup assertion (assertProgramsExist) verifies they exist on-chain when
 * a live RPC is configured.
 *
 * Verify against the official pump.fun docs and a block explorer before every
 * live deployment. Last verified: PENDING — confirm before Phase 1.
 */
export const PROGRAM_IDS = {
  /** pump.fun bonding-curve program (emits the migration instruction). */
  PUMP_FUN: '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P',
  /** PumpSwap AMM — the current default graduation venue. */
  PUMP_SWAP: 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA',
  /** Raydium AMM v4 — legacy graduation venue. */
  RAYDIUM_AMM: '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8',
  /** SPL Token program. */
  TOKEN: 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
  /** SPL Token-2022 program. */
  TOKEN_2022: 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb',
  /** System program. */
  SYSTEM: '11111111111111111111111111111111',
  /** Compute budget program (priority fee / CU limit ixs). */
  COMPUTE_BUDGET: 'ComputeBudget111111111111111111111111111111',
  /** Associated Token Account program (ATA creation in swaps). */
  ASSOCIATED_TOKEN: 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL',
  /** PumpSwap dynamic-fee program (referenced by swap instructions). */
  PUMP_FEE: 'pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ',
} as const satisfies Record<string, Address>;

/**
 * Programs the risk manager will permit the wallet to sign for (Section 8).
 * Any transaction touching a program outside this set is refused and logged.
 * Jito tip accounts are system-program transfers, so they are covered by SYSTEM.
 */
export const WHITELISTED_PROGRAM_IDS: readonly Address[] = [
  PROGRAM_IDS.PUMP_FUN,
  PROGRAM_IDS.PUMP_SWAP,
  PROGRAM_IDS.RAYDIUM_AMM,
  PROGRAM_IDS.TOKEN,
  PROGRAM_IDS.TOKEN_2022,
  PROGRAM_IDS.SYSTEM,
  PROGRAM_IDS.COMPUTE_BUDGET,
  PROGRAM_IDS.ASSOCIATED_TOKEN,
  PROGRAM_IDS.PUMP_FEE,
];

/** Wrapped SOL mint — quote asset for all graduation pools. */
export const WSOL_MINT: Address = 'So11111111111111111111111111111111111111112';

export const LAMPORTS_PER_SOL = 1_000_000_000;
