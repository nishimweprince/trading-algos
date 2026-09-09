import { describe, it, expect } from 'vitest';
import { checkSellability } from '../src/guardrails/checks/pending.ts';
import type { CheckContext } from '../src/guardrails/engine.ts';
import type { Candidate } from '../src/enrichment/types.ts';
import { PublicKey } from '@solana/web3.js';
import { classifySellabilityError, createIdempotentAtaInstruction } from '../src/executor/sellability.ts';
import { PROGRAM_IDS } from '../src/core/constants.ts';

function ctx(sellable?: Candidate['enrichment']['sellable']): CheckContext {
  return {
    candidate: {
      graduation: { mint: 'M', venue: 'pumpswap', poolAddress: '', slot: 1, feedSource: 'pumpportal', receivedAtNs: 0n },
      enrichment: { unknowns: [], elapsedMs: 1, ...(sellable ? { sellable } : {}) },
    },
    // config/repos/mode unused by checkSellability
    config: {} as CheckContext['config'],
    repos: {} as CheckContext['repos'],
    mode: 'live',
  };
}

describe('H4 checkSellability', () => {
  it('is unknown when the probe did not run', () => {
    const r = checkSellability(ctx());
    expect(r.status).toBe('unknown');
    expect(r.reason).toBe('not_run');
  });
  it('passes when the atomic buy+sell simulated cleanly', () => {
    expect(checkSellability(ctx({ status: 'pass', detail: 'ok' })).status).toBe('pass');
  });
  it('fails (honeypot) when the sell leg was rejected', () => {
    const r = checkSellability(ctx({ status: 'fail', reason: 'sell_failed', detail: 'sell leg failed' }));
    expect(r.status).toBe('fail');
    expect(r.reason).toBe('sell_failed');
    expect(r.detail).toContain('sell leg');
  });
  it('preserves an unfunded-wallet outcome as unknown', () => {
    const r = checkSellability(ctx({ status: 'unknown', reason: 'wallet_unfunded', detail: 'insufficient funds' }));
    expect(r.status).toBe('unknown');
    expect(r.reason).toBe('wallet_unfunded');
  });
  it('preserves tx-too-large unknowns for the guarded H4 bypass', () => {
    const r = checkSellability(ctx({ status: 'unknown', reason: 'tx_too_large', detail: 'VersionedTransaction too large' }));
    expect(r.status).toBe('unknown');
    expect(r.reason).toBe('tx_too_large');
  });
  it('preserves the buy-only backstop reason for the guarded relaxed lane', () => {
    const r = checkSellability(ctx({ status: 'unknown', reason: 'buy_only_ok', detail: 'buy leg simulated cleanly' }));
    expect(r.status).toBe('unknown');
    expect(r.reason).toBe('buy_only_ok');
  });

  it('classifies structural, funding, RPC, and true sell failures separately', () => {
    expect(classifySellabilityError({ message: 'VersionedTransaction too large' })).toBe('tx_too_large');
    expect(classifySellabilityError({ err: 'AccountNotFound' })).toBe('account_setup_unavailable');
    expect(classifySellabilityError({ err: 'InsufficientFunds' })).toBe('wallet_unfunded');
    expect(classifySellabilityError(new Error('fetch failed'), 'transport')).toBe('rpc_unavailable');
    expect(classifySellabilityError({ InstructionError: [9, 'Custom'] })).toBe('sell_failed');
  });

  it('builds a token-program-aware idempotent ATA setup instruction', () => {
    const owner = new PublicKey('11111111111111111111111111111111');
    const mint = new PublicKey('So11111111111111111111111111111111111111112');
    const tokenProgram = new PublicKey(PROGRAM_IDS.TOKEN);
    const setup = createIdempotentAtaInstruction(owner, owner, mint, tokenProgram);
    expect(setup.instruction.programId.toBase58()).toBe(PROGRAM_IDS.ASSOCIATED_TOKEN);
    expect(setup.instruction.data).toEqual(Buffer.from([1]));
    expect(setup.instruction.keys[1]?.pubkey.equals(setup.address)).toBe(true);
    expect(setup.instruction.keys.at(-1)?.pubkey.toBase58()).toBe(PROGRAM_IDS.TOKEN);
  });
});

/**
 * Regression: the 1232-byte overflow arrives from @solana/web3.js as a real
 * Error, and Error fields are non-enumerable — JSON.stringify(err) is `{}`.
 * Classifying it as `rpc_unavailable` instead of `tx_too_large` silently
 * disables tolerateTxTooLargeSellability AND sellabilityBuyOnlyBackstop, both
 * of which are gated on reason === 'tx_too_large'. In live mode that turns a
 * recoverable candidate into a hard UNKNOWN:H4 veto.
 *
 * Caught on devnet: a real probe returned `rpc_unavailable: encoding overruns
 * Uint8Array`. Every pre-existing case here passed a plain object, never an
 * Error, so the bug was invisible.
 */
describe('classifySellabilityError — Error instances (not just plain objects)', () => {
  it('classifies a real Error carrying the overflow message as tx_too_large', () => {
    expect(classifySellabilityError(new Error('encoding overruns Uint8Array'))).toBe('tx_too_large');
    expect(classifySellabilityError(new Error('VersionedTransaction too large'))).toBe('tx_too_large');
  });

  it('does not let the transport hint mask an overflow', () => {
    // This is the exact shape observed on devnet: thrown as an Error, caught in
    // the transport path. Size must win over the transport classification.
    expect(classifySellabilityError(new Error('encoding overruns Uint8Array'), 'transport')).toBe('tx_too_large');
  });

  it('finds the overflow through a wrapped cause', () => {
    const wrapped = new Error('probe assembly failed', { cause: new Error('encoding overruns Uint8Array') });
    expect(classifySellabilityError(wrapped)).toBe('tx_too_large');
  });

  it('still classifies genuine transport failures as rpc_unavailable', () => {
    expect(classifySellabilityError(new Error('fetch failed'), 'transport')).toBe('rpc_unavailable');
    expect(classifySellabilityError(new Error('429 Too Many Requests'))).toBe('rpc_unavailable');
  });

  it('keeps the existing plain-object behaviour', () => {
    expect(classifySellabilityError({ message: 'VersionedTransaction too large' })).toBe('tx_too_large');
    expect(classifySellabilityError({ InstructionError: [9, 'Custom'] })).toBe('sell_failed');
  });
});
