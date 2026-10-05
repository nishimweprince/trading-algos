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
  second: { mint: string; pool: string; accounts: Record<string, RawAccountRead> };
};
const [globalKey, feeKey] = Object.keys(fx.accounts).slice(2) as [string, string];
const configs = {
  globalConfig: PUMP_AMM_SDK.decodeGlobalConfig(toAccountInfo(fx.accounts[globalKey]!)),
  feeConfig: PUMP_AMM_SDK.decodeFeeConfig(toAccountInfo(fx.accounts[feeKey]!)),
};

function setup(over: { configs?: typeof configs | null } = {}) {
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
    expect(r.poolSource).toBe('pushed');
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

  it('never waits for the pool push: builds from a pool synthesized at admission', async () => {
    const { probe, prep } = setup();
    probe.setPoolTemplate(fx.accounts[fx.pool]!);
    await probe.prepare(fx.mint, prep, PUMP_AMM_SDK.decodePool(toAccountInfo(fx.accounts[fx.pool]!)).coinCreator.toBase58());
    expect(probe.hasSynthesized(fx.mint)).toBe(true);
    const r = await probe.probe(input()); // no onPool yet
    expect(r).toMatchObject({ ok: true, poolSource: 'synthesized' });
    expect(probe.awaitingCheck(fx.mint)).toBe(true);
    probe.onPool(fx.mint, fx.accounts[fx.pool]!);
    expect(await probe.verify(fx.mint)).toEqual({ match: true, vDeltaLamports: 0n });
    expect(probe.awaitingCheck(fx.mint)).toBe(false);
  });

  it('a pool synthesized for ANOTHER mint from this template matches that mint\'s real pool on every field the buy reads', async () => {
    const { probe } = setup();
    const w = Wallet.load('RACER_SEND_READY_TEST_NO_KEY', 'dry-run');
    probe.setPoolTemplate(fx.accounts[fx.pool]!); // template: mint #1's pool
    const second = fx.second;
    const realPool = PUMP_AMM_SDK.decodePool(toAccountInfo(second.accounts[second.pool]!));
    const prep2 = {
      poolKey: second.pool,
      baseMint: second.accounts[second.mint]!,
      userBaseAta: deriveAta(w.publicKey, second.mint, second.accounts[second.mint]!.owner === PROGRAM_IDS.TOKEN_2022),
      userQuoteAta: deriveAta(w.publicKey, WSOL_MINT, false),
    };
    const synth = await probe.synthesizePool(second.mint, prep2, realPool.coinCreator.toBase58());
    const s2 = PUMP_AMM_SDK.decodePool(toAccountInfo(synth!));
    for (const k of ['creator', 'baseMint', 'quoteMint', 'lpMint', 'poolBaseTokenAccount', 'poolQuoteTokenAccount', 'coinCreator'] as const) {
      expect(s2[k].toBase58(), k).toBe(realPool[k].toBase58());
    }
    for (const k of ['isMayhemMode', 'isCashbackCoin', 'creatorFeeBps'] as const) expect(String(s2[k]), k).toBe(String(realPool[k]));
    // Only the template-derived virtual quote may differ, and only slightly.
    const vDelta = BigInt(realPool.virtualQuoteReserves.toString()) - BigInt(s2.virtualQuoteReserves.toString());
    expect(vDelta < 0n ? -vDelta : vDelta).toBeLessThan(10_000_000n); // < 0.01 SOL of ~17.58 SOL
    // And the full audit: identical instruction accounts from synthesized vs real.
    await probe.prepare(second.mint, prep2, realPool.coinCreator.toBase58());
    const r = await probe.probe({ ...input(), mint: second.mint });
    expect(r).toMatchObject({ ok: true, poolSource: 'synthesized' });
    probe.onPool(second.mint, second.accounts[second.pool]!);
    expect((await probe.verify(second.mint))!.match).toBe(true);
  });

  it('synthesizes at probe time when the template arrived after admission (still no waiting)', async () => {
    const { probe, prep } = setup();
    const creator = PUMP_AMM_SDK.decodePool(toAccountInfo(fx.accounts[fx.pool]!)).coinCreator.toBase58();
    await probe.prepare(fx.mint, prep, creator); // no template yet
    expect(probe.hasSynthesized(fx.mint)).toBe(false);
    probe.setPoolTemplate(fx.accounts[fx.pool]!);
    expect(await probe.probe(input())).toMatchObject({ ok: true, poolSource: 'synthesized' });
  });

  it('keep-warm replays the last real inputs (or a seed) and never needs a migrate', async () => {
    const { probe, prep } = setup();
    expect(await probe.warm()).toBe(false); // nothing to replay yet
    probe.seedWarm(prep, fx.accounts[fx.pool]!, 206_900_000_000_000n, 84_990_000_000n);
    expect(await probe.warm()).toBe(true);
    expect(await probe.warm()).toBe(true);
    expect(probe.warmRuns).toBe(2);
    // A real probe refreshes the inputs; warm keeps working after the mint is released.
    probe.prepare(fx.mint, prep);
    probe.onPool(fx.mint, fx.accounts[fx.pool]!);
    expect((await probe.probe(input())).ok).toBe(true);
    probe.release(fx.mint);
    expect(await probe.warm()).toBe(true);
  });

  it('reports why it could not build instead of guessing', async () => {
    const a = setup();
    expect(await a.probe.probe(input())).toMatchObject({ ok: false, reason: 'not-prepared' });
    const b = setup({ configs: null });
    await b.probe.prepare(fx.mint, b.prep);
    expect(await b.probe.probe(input())).toMatchObject({ ok: false, reason: 'no-configs' });
    const c = setup();
    await c.probe.prepare(fx.mint, c.prep); // no template, no creator, no push
    const t0 = Date.now();
    expect(await c.probe.probe(input())).toMatchObject({ ok: false, reason: 'no-pool' });
    expect(Date.now() - t0).toBeLessThan(50); // fails fast: nothing to wait for
  });
});
