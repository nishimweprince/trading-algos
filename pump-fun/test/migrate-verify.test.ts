import { describe, expect, it } from 'vitest';
import {
  BUY_LOG,
  callerOf,
  callerSolDelta,
  callerTokenDeltas,
  graduatedMint,
  hasBuyLog,
  incentiveLogLines,
  isMigrateTx,
  rankCallers,
  summarizeMigrate,
  type MigrateSample,
  type ParsedTxWithLogs,
} from '../src/research/migrateVerify.ts';
import type { ParsedTx } from '../src/core/rpc.ts';

const CALLER = 'Caller111111111111111111111111111111111111111';
const OTHER = 'Other2222222222222222222222222222222222222222';
const MINT = 'Mint33333333333333333333333333333333333333333';
const WSOL = 'So11111111111111111111111111111111111111112';

function tx(over: { logs?: string[]; meta?: object; slot?: number } = {}): ParsedTxWithLogs {
  return {
    slot: over.slot ?? 100,
    blockTime: 1_700_000_000,
    meta: {
      err: null,
      fee: 5000,
      preBalances: [1_000_000_000, 0],
      postBalances: [999_995_000, 0],
      logMessages: over.logs ?? ['Program log: Instruction: MigrateV2'],
      ...(over.meta ?? {}),
    },
    transaction: {
      signatures: ['sig'],
      message: { accountKeys: [{ pubkey: CALLER, signer: true }, { pubkey: OTHER, signer: false }] },
    },
  } as ParsedTxWithLogs;
}

describe('isMigrateTx', () => {
  it('accepts Migrate and MigrateV2 logs', () => {
    expect(isMigrateTx(tx({ logs: ['Program 6EF8... invoke [1]', 'Program log: Instruction: Migrate'] }))).toBe(true);
    expect(isMigrateTx(tx())).toBe(true);
  });
  it('rejects the creator-config migration and failed txs', () => {
    expect(isMigrateTx(tx({ logs: ['Program log: Instruction: MigrateBondingCurveCreator'] }))).toBe(false);
    expect(isMigrateTx(tx({ logs: ['Program log: Instruction: Buy'] }))).toBe(false);
    expect(isMigrateTx(tx({ meta: { err: { InstructionError: [0, 'Custom'] } } as never }))).toBe(false);
  });
});

describe('callerOf / callerSolDelta', () => {
  it('attributes the fee payer at index 0 and adds the fee back for R', () => {
    const t = tx();
    expect(callerOf(t)).toBe(CALLER);
    // delta -5000 (fee only) → net of fee 0: no payout.
    expect(callerSolDelta(t)).toEqual({ deltaLamports: -5000, feeLamports: 5000, netOfFeeLamports: 0 });
  });
  it('surfaces a positive net-of-fee as received value', () => {
    const t = tx({ meta: { preBalances: [1_000_000_000], postBalances: [1_005_000_000], fee: 5000 } as never });
    expect(callerSolDelta(t)).toEqual({
      deltaLamports: 5_000_000,
      feeLamports: 5000,
      netOfFeeLamports: 5_005_000,
    });
  });
});

describe('callerTokenDeltas', () => {
  const minted = {
    preTokenBalances: [],
    postTokenBalances: [{ accountIndex: 1, mint: MINT, owner: CALLER, uiTokenAmount: { amount: '250', decimals: 6 } }],
  } as never;
  it('reports caller-owned gains and skips others-owned rows', () => {
    expect(callerTokenDeltas(tx({ meta: minted }), CALLER)).toEqual([
      { mint: MINT, owner: CALLER, deltaRaw: '250', decimals: 6 },
    ]);
    expect(callerTokenDeltas(tx({ meta: minted }), OTHER)).toEqual([]);
  });
  it('keeps owner-less rows as unattributed instead of dropping them', () => {
    const noOwner = {
      preTokenBalances: [],
      postTokenBalances: [{ accountIndex: 1, mint: MINT, uiTokenAmount: { amount: '7', decimals: 0 } }],
    } as never;
    expect(callerTokenDeltas(tx({ meta: noOwner }), CALLER)).toEqual([
      { mint: MINT, owner: null, deltaRaw: '7', decimals: 0 },
    ]);
  });
  it('covers accounts closed within the tx (pre-only)', () => {
    const closed = {
      preTokenBalances: [{ accountIndex: 1, mint: MINT, owner: CALLER, uiTokenAmount: { amount: '9', decimals: 0 } }],
      postTokenBalances: [],
    } as never;
    expect(callerTokenDeltas(tx({ meta: closed }), CALLER)).toEqual([
      { mint: MINT, owner: CALLER, deltaRaw: '-9', decimals: 0 },
    ]);
  });
});

describe('graduatedMint', () => {
  it('returns the single non-WSOL mint and null when ambiguous', () => {
    const one = {
      preTokenBalances: [],
      postTokenBalances: [
        { accountIndex: 1, mint: MINT, uiTokenAmount: { amount: '1', decimals: 6 } },
        { accountIndex: 2, mint: WSOL, uiTokenAmount: { amount: '2', decimals: 9 } },
      ],
    } as never;
    expect(graduatedMint(tx({ meta: one }))).toEqual({ mint: MINT, candidateMints: [MINT] });
    expect(graduatedMint(tx())).toEqual({ mint: null, candidateMints: [] });
  });
});

describe('buy / incentive matchers', () => {
  it('matches Buy/BuyV2 and rejects wrapper names', () => {
    expect(BUY_LOG.test('Program log: Instruction: Buy')).toBe(true);
    expect(hasBuyLog(['Program log: Instruction: BuyV2'])).toBe(true);
    expect(hasBuyLog(['Program log: Instruction: CreateCoinAndBuyBondingCurveV3'])).toBe(false);
  });
  it('finds incentive lines case-insensitively', () => {
    expect(incentiveLogLines(['Program log: Instruction: ClaimTokenIncentives'])).toHaveLength(1);
    expect(incentiveLogLines(['Program log: Instruction: Buy'])).toHaveLength(0);
  });
});

describe('summarizeMigrate / rankCallers', () => {
  const sample = (caller: string, netOfFee: number, migrates = 1): MigrateSample[] =>
    Array.from({ length: migrates }, (_, i) => ({
      signature: `${caller}-${i}`,
      slot: 100 + i,
      blockTime: null,
      caller,
      sol: { deltaLamports: netOfFee - 5000, feeLamports: 5000, netOfFeeLamports: netOfFee },
      tokenDeltas: [],
      graduatedMint: null,
      candidateMints: [],
    }));
  it('rejects non-migrates and ranks by count with SOL aggregation', () => {
    expect(summarizeMigrate('s', tx({ logs: ['Program log: Instruction: Buy'] }))).toBeNull();
    const ranked = rankCallers([...sample(CALLER, 0, 3), ...sample(OTHER, 1_000_000, 1)]);
    expect(ranked.map((c) => c.caller)).toEqual([CALLER, OTHER]);
    expect(ranked[0]).toMatchObject({ migrates: 3, totalNetOfFeeLamports: 0, netPositiveCount: 0 });
    expect(ranked[1]).toMatchObject({ migrates: 1, totalNetOfFeeLamports: 1_000_000, netPositiveCount: 1 });
  });
});

describe('types', () => {
  it('ParsedTx fixtures satisfy the core subset', () => {
    const t: ParsedTx = tx();
    expect(t.slot).toBe(100);
  });
});
