import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  PublicKey,
  SystemProgram,
  TransactionMessage,
  VersionedTransaction,
} from '@solana/web3.js';
import { fetchCurveTradeTx, verifyCurveTradeTx, CurveTradeError } from '../src/executor/curve.ts';

const WALLET = '4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf';
const MINT = 'GVWpUoAccWdGqiVmXYdTMjhW8NT5yMqLLzY7zx4Rpump';
const BLOCKHASH = '11111111111111111111111111111111';

/** Minimal synthetic versioned tx: payer + mint referenced + one transfer ix. */
function makeTx(payer: string, includeMint: boolean): Uint8Array {
  const payerPk = new PublicKey(payer);
  const ixs = [
    SystemProgram.transfer({ fromPubkey: payerPk, toPubkey: payerPk, lamports: 1 }),
  ];
  if (includeMint) {
    ixs.push(
      SystemProgram.transfer({ fromPubkey: payerPk, toPubkey: new PublicKey(MINT), lamports: 0 }),
    );
  }
  const msg = new TransactionMessage({
    payerKey: payerPk,
    recentBlockhash: BLOCKHASH,
    instructions: ixs,
  }).compileToV0Message();
  return new Uint8Array(new VersionedTransaction(msg).serialize());
}

describe('verifyCurveTradeTx', () => {
  it('accepts a well-formed trade tx', () => {
    const txBytes = makeTx(WALLET, true);
    expect(verifyCurveTradeTx({ txBytes, side: 'buy', mint: MINT }, WALLET)).toEqual({ ok: true });
  });

  it('rejects payer mismatch, missing mint, and garbage', () => {
    const other = '8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt';
    expect(verifyCurveTradeTx({ txBytes: makeTx(WALLET, true), side: 'buy', mint: MINT }, other).reason).toBe(
      'payer-mismatch',
    );
    expect(verifyCurveTradeTx({ txBytes: makeTx(WALLET, false), side: 'buy', mint: MINT }, WALLET).reason).toBe(
      'mint-missing',
    );
    expect(verifyCurveTradeTx({ txBytes: new Uint8Array([1, 2, 3]), side: 'buy', mint: MINT }, WALLET).reason).toBe(
      'unparseable-transaction',
    );
  });
});

describe('fetchCurveTradeTx', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('maps buy params and parses unsigned bytes', async () => {
    const txBytes = makeTx(WALLET, true);
    let seenBody: Record<string, unknown> = {};
    const fakeFetch = (async (_url: string, init: { body: string }) => {
      seenBody = JSON.parse(init.body) as Record<string, unknown>;
      return new Response(txBytes, { status: 200 });
    }) as unknown as typeof fetch;
    const out = await fetchCurveTradeTx(
      { wallet: WALLET, mint: MINT, side: 'buy', amountSol: 0.05, slippagePct: 5, priorityFeeSol: 0.00001 },
      fakeFetch,
    );
    expect(seenBody).toMatchObject({
      publicKey: WALLET,
      action: 'buy',
      mint: MINT,
      denominatedInSol: 'true',
      amount: 0.05,
      slippage: 5,
      pool: 'pump',
    });
    expect(out.txBytes).toEqual(txBytes);
  });

  it('maps sell size in base units and surfaces HTTP errors', async () => {
    let seenBody: Record<string, unknown> = {};
    const fakeFetch = (async (_url: string, init: { body: string }) => {
      seenBody = JSON.parse(init.body) as Record<string, unknown>;
      return new Response('nope', { status: 422 });
    }) as unknown as typeof fetch;
    await expect(
      fetchCurveTradeTx(
        { wallet: WALLET, mint: MINT, side: 'sell', tokenAmount: 1000n, slippagePct: 5, priorityFeeSol: 0.00001 },
        fakeFetch,
      ),
    ).rejects.toBeInstanceOf(CurveTradeError);
    expect(seenBody['denominatedInSol']).toBe('false');
    expect(seenBody['amount']).toBe('1000');
  });

  it('rejects invalid sizes before any request', async () => {
    const neverFetch = (async () => {
      throw new Error('must not be called');
    }) as unknown as typeof fetch;
    await expect(
      fetchCurveTradeTx({ wallet: WALLET, mint: MINT, side: 'buy', slippagePct: 5, priorityFeeSol: 0 }, neverFetch),
    ).rejects.toBeInstanceOf(CurveTradeError);
  });
});
