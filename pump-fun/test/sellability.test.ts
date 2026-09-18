import { describe, it, expect } from 'vitest';
import { checkSellability } from '../src/guardrails/checks/pending.ts';
import type { CheckContext } from '../src/guardrails/engine.ts';
import type { Candidate } from '../src/enrichment/types.ts';
import { PublicKey } from '@solana/web3.js';
import { classifySellabilityError, createIdempotentAtaInstruction, instructionErrorIndex, probeLayout } from '../src/executor/sellability.ts';
import { TransactionInstruction } from '@solana/web3.js';
import { PROGRAM_IDS } from '../src/core/constants.ts';
import { entryMovePct, EntryMoveExceeded } from '../src/executor/slippage.ts';

function ctx(sellable?: Candidate['enrichment']['sellable']): CheckContext {
  return {
    candidate: {
      graduation: { mint: 'M', venue: 'pumpswap', poolAddress: '', slot: 1, feedSource: 'pumpportal', receivedAtNs: 0n },
      enrichment: { unknowns: [], elapsedMs: 1, ...(sellable ? { sellable } : {}) },
    },
    // config/repos/mode unused by checkSellability
    config: { guardrails: {} } as unknown as CheckContext['config'],
    repos: {} as CheckContext['repos'],
    mode: 'live',
    walletSol: 0,
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
    // The exact payload recorded on 2026-09-17 for 37 of 100 candidates: Pump
    // AMM ExceededSlippage at instruction 7. A moving pool, not a honeypot and
    // not an account-setup problem.
    expect(classifySellabilityError({ InstructionError: [7, { Custom: 6004 }] })).toBe('price_moved');
    expect(classifySellabilityError({ err: { InstructionError: [6, { Custom: 6004 }] } })).toBe('price_moved');
  });

  it('attributes InstructionErrors by leg when given the probe layout', () => {
    // Live layouts observed 2026-09-18: buy at 6 (plain) or 7 (fresh pool needs
    // the SDK's extendAccount at 2), sell 3 ixs later.
    const layout = { buyIx: 7, sellIx: 10 };
    // extendAccount / ATA-create failures before the buy are setup problems,
    // not honeypots — this exact payload was a hard H4 fail on healthy pools.
    expect(classifySellabilityError({ InstructionError: [2, { Custom: 2004 }] }, 'simulation', layout)).toBe('account_setup_unavailable');
    expect(classifySellabilityError({ InstructionError: [7, { Custom: 6004 }] }, 'simulation', layout)).toBe('price_moved');
    expect(classifySellabilityError({ InstructionError: [7, { Custom: 6001 }] }, 'simulation', layout)).toBe('buy_failed');
    expect(classifySellabilityError({ InstructionError: [10, { Custom: 1 }] }, 'simulation', layout)).toBe('sell_failed');
    expect(classifySellabilityError({ InstructionError: [11, { Custom: 1 }] }, 'simulation', layout)).toBe('sell_failed');
    // Transport errors and non-instruction errors keep the text rules.
    expect(classifySellabilityError(new Error('fetch failed'), 'transport', layout)).toBe('rpc_unavailable');
    expect(classifySellabilityError({ message: 'VersionedTransaction too large' }, 'simulation', layout)).toBe('tx_too_large');
    expect(instructionErrorIndex({ err: { InstructionError: [9, { Custom: 1 }] } })).toBe(9);
    expect(instructionErrorIndex(new Error('boom'))).toBeUndefined();
  });

  it('locates the swap ixs by discriminator, skipping extendAccount and the ATA/WSOL prefix', () => {
    const pump = new PublicKey(PROGRAM_IDS.PUMP_SWAP);
    const other = new PublicKey(PROGRAM_IDS.TOKEN);
    const ix = (programId: PublicKey, disc: string) =>
      new TransactionInstruction({ programId, keys: [], data: Buffer.from(disc.padEnd(16, '0'), 'hex') });
    const extend = ix(pump, 'deadbeefdeadbeef');
    const buy = ix(pump, '66063d1201daebea');
    const sell = ix(pump, '33e685a4017f83ad');
    const misc = ix(other, '01');
    // [extend, ata, ata, transfer, sync, ata, buy, close, ata, sell, close] -> +2 compute-budget ixs
    expect(probeLayout([extend, misc, misc, misc, misc, buy, misc, misc, sell, misc])).toEqual({ buyIx: 7, sellIx: 10 });
    expect(probeLayout([misc, misc, misc, misc, buy, misc, misc, sell, misc])).toEqual({ buyIx: 6, sellIx: 9 });
    expect(probeLayout([misc, buy])).toBeUndefined();
  });

  it('turns an explicit early-move cap into a price_moved unknown', () => {
    const c = ctx({ status: 'pass', detail: 'clean', poolMovePct: 42 });
    c.config = { guardrails: { maxProbeMovePct: 30 } } as unknown as CheckContext['config'];
    const r = checkSellability(c);
    expect(r.status).toBe('unknown');
    expect(r.reason).toBe('price_moved');
    expect(r.detail).toContain('42.0%');
    c.config = { guardrails: { maxProbeMovePct: 60 } } as unknown as CheckContext['config'];
    expect(checkSellability(c).status).toBe('pass');
    c.config = { guardrails: {} } as unknown as CheckContext['config'];
    expect(checkSellability(c).status).toBe('pass');
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

describe('entry move gate (buy-build vs verdict snapshot)', () => {
  const ref = { baseReserve: 1_000_000n, quoteReserveLamports: 70_000_000_000n };
  it('measures the mid move from the verdict snapshot to the buy quote state', () => {
    expect(entryMovePct(ref, ref)).toBeCloseTo(0, 6);
    // 2026-09-18 first live entry: quote 70.3 -> ~73 SOL while base fell — mid +32.5%
    expect(entryMovePct(ref, { baseReserve: 755_000n, quoteReserveLamports: 70_000_000_000n })).toBeCloseTo(32.45, 1);
    expect(entryMovePct(ref, { baseReserve: 1_100_000n, quoteReserveLamports: 63_000_000_000n })).toBeCloseTo(-18.18, 1);
  });
  it('is undefined on unusable reserves rather than gating on garbage', () => {
    expect(entryMovePct({ baseReserve: 0n, quoteReserveLamports: 1n }, ref)).toBeUndefined();
    expect(entryMovePct(ref, { baseReserve: 1n, quoteReserveLamports: 0n })).toBeUndefined();
  });
  it('carries the numbers on the error the position manager records', () => {
    const e = new EntryMoveExceeded(32.4, 20);
    expect(e.movePct).toBe(32.4);
    expect(e.capPct).toBe(20);
    expect(e.message).toContain('+32.4%');
    expect(e.message).toContain('20%');
  });
});
