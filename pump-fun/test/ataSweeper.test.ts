import { describe, expect, it } from 'vitest';
import { Keypair, PublicKey } from '@solana/web3.js';
import { closeAccountInstruction, listEmptyTokenAccounts } from '../src/executor/ataSweeper.ts';
import { PROGRAM_IDS } from '../src/core/constants.ts';
import { heliusApiKeyFromUrl } from '../src/config/load.ts';

describe('ATA sweeper', () => {
  it('encodes SPL CloseAccount with rent back to the owner and the owner as signer', () => {
    const owner = Keypair.generate().publicKey;
    const acct = Keypair.generate().publicKey.toBase58();
    const ix = closeAccountInstruction(acct, owner, PROGRAM_IDS.TOKEN_2022);
    expect(ix.programId.toBase58()).toBe(PROGRAM_IDS.TOKEN_2022);
    expect([...ix.data]).toEqual([9]);
    expect(ix.keys[0]).toMatchObject({ pubkey: new PublicKey(acct), isWritable: true, isSigner: false });
    expect(ix.keys[1]).toMatchObject({ pubkey: owner, isWritable: true });
    expect(ix.keys[2]).toMatchObject({ pubkey: owner, isSigner: true });
  });

  it('lists only zero-balance accounts, across both token programs', async () => {
    const owner = Keypair.generate().publicKey;
    const mk = (amount: string, lamports = 2_039_280) => ({
      pubkey: Keypair.generate().publicKey,
      account: { lamports, data: { parsed: { info: { mint: 'M', tokenAmount: { amount } } } } },
    });
    const byProgram: Record<string, unknown[]> = {
      [PROGRAM_IDS.TOKEN]: [mk('0'), mk('150')],
      [PROGRAM_IDS.TOKEN_2022]: [mk('0'), mk('0')],
    };
    const connection = {
      getParsedTokenAccountsByOwner: async (_o: PublicKey, f: { programId: PublicKey }) => ({
        value: byProgram[f.programId.toBase58()] ?? [],
      }),
    };
    const empties = await listEmptyTokenAccounts(connection as never, owner);
    expect(empties).toHaveLength(3);
    expect(empties.filter((e) => e.programId === PROGRAM_IDS.TOKEN_2022)).toHaveLength(2);
    expect(empties.every((e) => e.lamports === 2_039_280)).toBe(true);
  });
});

describe('heliusApiKeyFromUrl', () => {
  it('extracts the api-key query param and tolerates junk', () => {
    expect(heliusApiKeyFromUrl('https://mainnet.helius-rpc.com/?api-key=abc-123')).toBe('abc-123');
    expect(heliusApiKeyFromUrl('https://rpc.ankr.com/solana')).toBeUndefined();
    expect(heliusApiKeyFromUrl('not a url')).toBeUndefined();
    expect(heliusApiKeyFromUrl(undefined)).toBeUndefined();
  });
});
