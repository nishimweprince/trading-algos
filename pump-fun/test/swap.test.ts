import { describe, it, expect } from 'vitest';
import { PublicKey } from '@solana/web3.js';
import { buildBuyInstruction, buildSellInstruction } from '../src/executor/swap.ts';
import { WSOL_MINT } from '../src/core/constants.ts';
import type { PoolInfo } from '../src/enrichment/pool.ts';

/**
 * Golden fixture from a real on-chain BUY (sig kBvhSJgQrJwD…): pool 8fkXSNXp,
 * user 2mw9Th6u, Token-2022 base mint 64W4…pump. The builder must reproduce this
 * exact ordered account list — this pins the account layout + every PDA seed to
 * verified ground truth.
 */
const REAL_BUY_ACCOUNTS = [
  '8fkXSNXpC7UEdKY5ZnHrFyQvTrtWYN7omGXfY4L68fkr',
  '2mw9Th6unQLVhVEaseSen5cxQxdzoQBp6QDstVgfP7w7',
  'ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw',
  '64W4CqYgGzco1Rm5cgHepUWPvqPWWTJ6NRRWwLVGpump',
  'So11111111111111111111111111111111111111112',
  'FMQfrhan5p24xgmhmu3vvogWagnMV8EriPNHJF5SqoPQ',
  '73WxEQX63hizVSwuMjVEGhJSE51mufznSqPHcqvUrzkh',
  'BdLJ7GGh6MXfvjujMmMf8yH1faRRtcMhLHmpEy3afW46',
  '5sUsJXt7D5jmdydhDQA6BxATz6wpq9fGBcV6ukYT8Roj',
  '62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV',
  '94qWNrtmfn42h3ZjUZwWvK1MEo9uVmmrBPd2hpNjYDjb',
  'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb',
  'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
  '11111111111111111111111111111111',
  'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL',
  'GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR',
  'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA',
  '24zsLaJoj3LHerXZo5HAvc8pmKjPbc4nWkvFdqwn1grg',
  'E4f2qDgjne99HcBVr9z6gjXo9C5hnD6N8W3GZeDXcTSw',
  'C2aFPdENg4A2HQsmrd5rTw5TaYBX5Ku887cWjbFKtZpw',
  '5UvoHNCcUcAtfSxtxizhkBbmhN1hzSbmUwz4SHhESRNT',
  '5PHirr8joyTMp9JMm6nW7hNDVyEYdkzDqazxPD7RaTjx',
  'pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ',
];

const POOL: PoolInfo = {
  poolAddress: '8fkXSNXpC7UEdKY5ZnHrFyQvTrtWYN7omGXfY4L68fkr',
  baseMint: '64W4CqYgGzco1Rm5cgHepUWPvqPWWTJ6NRRWwLVGpump',
  quoteMint: WSOL_MINT,
  lpMint: 'EgQXyoPxMrFAqSHDRSxhDeBJNe67smkAz5BP8WwMvH45',
  baseVault: 'BdLJ7GGh6MXfvjujMmMf8yH1faRRtcMhLHmpEy3afW46',
  quoteVault: '5sUsJXt7D5jmdydhDQA6BxATz6wpq9fGBcV6ukYT8Roj',
  creator: 'C',
  coinCreator: 'Dt4gYgAUts6puim6yj4m874V5TuGUGFyZVv4RNpFvUkY',
  isCanonical: true,
  baseReserve: 1000n,
  quoteReserveLamports: 45n,
  lpMintSupply: 0n,
};
const USER = new PublicKey('2mw9Th6unQLVhVEaseSen5cxQxdzoQBp6QDstVgfP7w7');

describe('buildBuyInstruction', () => {
  it('reproduces the real transaction account list exactly', () => {
    const ix = buildBuyInstruction({
      pool: POOL,
      user: USER,
      baseIsToken2022: true, // 64W4…pump is a Token-2022 mint
      baseAmountOut: 1000n,
      maxQuoteIn: 500n,
    });
    expect(ix.keys.map((k) => k.pubkey.toBase58())).toEqual(REAL_BUY_ACCOUNTS);
    expect(ix.programId.toBase58()).toBe('pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA');
  });

  it('encodes the discriminator and u64 args', () => {
    const ix = buildBuyInstruction({ pool: POOL, user: USER, baseIsToken2022: true, baseAmountOut: 7n, maxQuoteIn: 9n });
    expect([...ix.data.subarray(0, 8)]).toEqual([102, 6, 61, 18, 1, 218, 235, 234]);
    expect(ix.data.readBigUInt64LE(8)).toBe(7n);
    expect(ix.data.readBigUInt64LE(16)).toBe(9n);
  });

  it('marks the correct accounts writable/signer', () => {
    const ix = buildBuyInstruction({ pool: POOL, user: USER, baseIsToken2022: true, baseAmountOut: 1n, maxQuoteIn: 1n });
    expect(ix.keys[1]!.isSigner).toBe(true); // user
    expect(ix.keys[0]!.isWritable).toBe(true); // pool
    expect(ix.keys[2]!.isWritable).toBe(false); // global_config
  });
});

describe('buildSellInstruction', () => {
  it('produces 21 accounts (buy minus the volume accumulators)', () => {
    const ix = buildSellInstruction({ pool: POOL, user: USER, baseIsToken2022: true, baseAmountIn: 100n, minQuoteOut: 1n });
    expect(ix.keys).toHaveLength(21);
    expect([...ix.data.subarray(0, 8)]).toEqual([51, 230, 133, 164, 1, 127, 131, 173]);
    // First 19 accounts match the buy layout through coin_creator_vault_authority.
    expect(ix.keys.slice(0, 19).map((k) => k.pubkey.toBase58())).toEqual(REAL_BUY_ACCOUNTS.slice(0, 19));
  });
});
