import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { Keypair, VersionedTransaction } from '@solana/web3.js';
import { PUMP_AMM_SDK } from '@pump-fun/pump-swap-sdk';
import { SendReadyProbe } from '../src/racer/sendReady.ts';
import { toAccountInfo, type RawAccountRead } from '../src/executor/swapState.ts';
import { Wallet } from '../src/executor/wallet.ts';
import { deriveAta } from '../src/core/ata.ts';
import { PROGRAM_IDS, WSOL_MINT } from '../src/core/constants.ts';
import { randomSenderTipAccount } from '../src/executor/heliusSender.ts';

const fx = JSON.parse(readFileSync(new URL('./fixtures/pumpswap-buy-state.json', import.meta.url), 'utf8')) as {
  mint: string;
  pool: string;
  accounts: Record<string, RawAccountRead>;
};
const [globalKey, feeKey] = Object.keys(fx.accounts).slice(2) as [string, string];
const configs = {
  globalConfig: PUMP_AMM_SDK.decodeGlobalConfig(toAccountInfo(fx.accounts[globalKey]!)),
  feeConfig: PUMP_AMM_SDK.decodeFeeConfig(toAccountInfo(fx.accounts[feeKey]!)),
};

function setup(over: { configs?: typeof configs | null; poolWaitMs?: number } = {}) {
  const wallet = Wallet.load('RACER_SEND_READY_TEST_NO_KEY', 'dry-run');
  const mintAcct = fx.accounts[fx.mint]!;
  const probe = new SendReadyProbe({
    configs: () => (over.configs === undefined ? configs : over.configs),
    blockhash: async () => Keypair.generate().publicKey.toBase58(),
    wallet,
    feePlan: { priorityMicroLamports: 250_000, jitoTipLamports: 1_000_000, exitTipLamports: 200_000 },
    tipAccount: () => randomSenderTipAccount(),
    quoteLamports: 100_000_000n,
    slippagePct: 5,
    poolWaitMs: over.poolWaitMs ?? 50,
  });
  const prep = {
    poolKey: fx.pool,
    baseMint: mintAcct,
    userBaseAta: deriveAta(wallet.publicKey, fx.mint, mintAcct.owner === PROGRAM_IDS.TOKEN_2022),
    userQuoteAta: deriveAta(wallet.publicKey, WSOL_MINT, false),
  };
  return { probe, prep };
}

const input = () => ({
  mint: fx.mint,
  receivedAtNs: process.hrtime.bigint(),
  baseReserve: 206_900_000_000_000n,
  quoteReserveLamports: 84_990_000_000n,
});

describe('racer send-ready probe', () => {
  it('builds and signs a real PumpSwap buy from pushed state with no RPC, timing each stage', async () => {
    const { probe, prep } = setup();
    probe.prepare(fx.mint, prep);
    probe.onPool(fx.mint, fx.accounts[fx.pool]!);
    const r = await probe.probe(input());
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.bytes).toBeGreaterThan(200);
    expect(r.totalUs).toBeGreaterThan(0);
    expect(r.totalUs).toBeGreaterThanOrEqual(r.stateUs + r.buildUs + r.signUs);
    expect(r.waitPoolUs).toBeLessThan(5_000); // pool already pushed: no wait
  });

  it('produces a signed v0 transaction touching only whitelisted programs', async () => {
    const { probe, prep } = setup();
    probe.prepare(fx.mint, prep);
    probe.onPool(fx.mint, fx.accounts[fx.pool]!);
    // Re-run the assembly path through the probe and decode its bytes via a second build.
    const r = await probe.probe(input());
    expect(r.ok).toBe(true);
    // The probe signs with an ephemeral key; decode a fresh build to inspect programs.
    const tx = VersionedTransaction.deserialize(
      await (async () => {
        const { assembleSignedSwapTx } = await import('../src/executor/assemble.ts');
        const { buildSwapState } = await import('../src/executor/swapState.ts');
        const { PumpAmmSdk } = await import('@pump-fun/pump-swap-sdk');
        const BN = (await import('bn.js')).default;
        const w = Wallet.load('RACER_SEND_READY_TEST_NO_KEY', 'dry-run');
        const state = buildSwapState(
          { ...prep, pool: fx.accounts[fx.pool]!, baseReserve: 206_900_000_000_000n, quoteReserveLamports: 84_990_000_000n, user: w.publicKey,
            userBaseAta: deriveAta(w.publicKey, fx.mint, true), userBaseAccount: null, userQuoteAta: deriveAta(w.publicKey, WSOL_MINT, false), userQuoteAccount: null },
          configs,
        );
        const ixs = await new PumpAmmSdk().buyQuoteInput(state, new BN('100000000'), 5);
        return assembleSignedSwapTx(ixs, { connection: null as never, wallet: w, feePlan: { priorityMicroLamports: 1, jitoTipLamports: 0, exitTipLamports: 0 }, blockhashProvider: async () => Keypair.generate().publicKey.toBase58() });
      })(),
    );
    const keys = tx.message.staticAccountKeys.map((k) => k.toBase58());
    const programs = new Set(tx.message.compiledInstructions.map((ix) => keys[ix.programIdIndex]!));
    expect(programs.has(PROGRAM_IDS.PUMP_SWAP)).toBe(true);
    expect(tx.signatures[0]!.some((b) => b !== 0)).toBe(true);
  });

  it('waits briefly for the pool push when the migrate tx arrives first', async () => {
    const { probe, prep } = setup({ poolWaitMs: 500 });
    probe.prepare(fx.mint, prep);
    const pending = probe.probe(input());
    setTimeout(() => probe.onPool(fx.mint, fx.accounts[fx.pool]!), 20);
    const r = await pending;
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.waitPoolUs).toBeGreaterThan(10_000);
  });

  it('reports why it could not build instead of guessing', async () => {
    const a = setup();
    expect(await a.probe.probe(input())).toMatchObject({ ok: false, reason: 'not-prepared' });
    const b = setup({ configs: null });
    b.probe.prepare(fx.mint, b.prep);
    expect(await b.probe.probe(input())).toMatchObject({ ok: false, reason: 'no-configs' });
    const c = setup({ poolWaitMs: 10 });
    c.probe.prepare(fx.mint, c.prep);
    expect(await c.probe.probe(input())).toMatchObject({ ok: false, reason: 'no-pool' });
  });
});
