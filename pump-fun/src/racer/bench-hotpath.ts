/**
 * Hot-path bench: screen → state → build → sign for a post-migration PumpSwap buy,
 * from the mainnet fixture (no RPC, nothing sent). Run on the target box,
 * pinned to the racer's core:
 *
 *   taskset -c 0 npm run bench:hotpath -- [iterations=500]
 */
import { readFileSync } from 'node:fs';
import { Keypair } from '@solana/web3.js';
import { PUMP_AMM_SDK } from '@pump-fun/pump-swap-sdk';
import { SendReadyProbe } from './sendReady.ts';
import { HotScreen } from './hotScreen.ts';
import { ConfigSchema } from '../config/schema.ts';
import { openDb } from '../persistence/db.ts';
import { Repositories } from '../persistence/repositories.ts';
import { toAccountInfo, type RawAccountRead } from '../executor/swapState.ts';
import { Wallet } from '../executor/wallet.ts';
import { deriveAta } from '../core/ata.ts';
import { PROGRAM_IDS, WSOL_MINT } from '../core/constants.ts';
import { randomSenderTipAccount } from '../executor/heliusSender.ts';

const n = Number(process.argv[2] ?? 500);
const fx = JSON.parse(readFileSync(new URL('../../test/fixtures/pumpswap-buy-state.json', import.meta.url), 'utf8')) as {
  mint: string;
  pool: string;
  accounts: Record<string, RawAccountRead>;
};
const [globalKey, feeKey] = Object.keys(fx.accounts).slice(2) as [string, string];
const configs = {
  globalConfig: PUMP_AMM_SDK.decodeGlobalConfig(toAccountInfo(fx.accounts[globalKey]!)),
  feeConfig: PUMP_AMM_SDK.decodeFeeConfig(toAccountInfo(fx.accounts[feeKey]!)),
};
const wallet = Wallet.load('RACER_BENCH_NO_KEY', 'dry-run');
const blockhash = Keypair.generate().publicKey.toBase58();
const probe = new SendReadyProbe({
  configs: () => configs,
  blockhash: async () => blockhash,
  wallet,
  feePlan: { priorityMicroLamports: 250_000, jitoTipLamports: 1_000_000, exitTipLamports: 200_000 },
  tipAccount: () => randomSenderTipAccount(),
  quoteLamports: 100_000_000n,
  slippagePct: 5,
});
probe.prepare(fx.mint, {
  poolKey: fx.pool,
  baseMint: fx.accounts[fx.mint]!,
  userBaseAta: deriveAta(wallet.publicKey, fx.mint, fx.accounts[fx.mint]!.owner === PROGRAM_IDS.TOKEN_2022),
  userQuoteAta: deriveAta(wallet.publicKey, WSOL_MINT, false),
});
probe.onPool(fx.mint, fx.accounts[fx.pool]!);

// The decision-time screen, prepared exactly as at admission (in-memory DB).
const hot = new HotScreen(ConfigSchema.parse({ mode: 'dry-run' }), new Repositories(openDb({ path: ':memory:', memory: true })));
hot.prepare(fx.mint, {
  creator: PUMP_AMM_SDK.decodePool(toAccountInfo(fx.accounts[fx.pool]!)).coinCreator.toBase58(),
  isToken2022: fx.accounts[fx.mint]!.owner === PROGRAM_IDS.TOKEN_2022,
  transferFee: null,
});
const screen = ({ virtualQuoteLamports }: { virtualQuoteLamports: bigint }) =>
  hot.screen({ mint: fx.mint, poolAddress: fx.pool, migrateSlot: 1, vaultQuoteLamports: 67_400_000_000n, virtualQuoteLamports }).accept;

const runs: Array<{ total: number; screen: number; state: number; build: number; sign: number }> = [];
for (let i = 0; i < n + 50; i++) {
  const r = await probe.probe({ mint: fx.mint, receivedAtNs: process.hrtime.bigint(), baseReserve: 206_900_000_000_000n, quoteReserveLamports: 84_990_000_000n, screen });
  if (!r.ok) throw new Error(`probe failed: ${r.reason}`);
  if (i >= 50) runs.push({ total: r.totalUs, screen: r.screenUs ?? 0, state: r.stateUs, build: r.buildUs, sign: r.signUs });
}
const q = (k: keyof (typeof runs)[number], p: number) => {
  const v = runs.map((r) => r[k]).sort((a, b) => a - b);
  return v[Math.min(v.length - 1, Math.floor(v.length * p))]!;
};
const fmt = (k: keyof (typeof runs)[number]) => `${k.padEnd(5)} p50 ${(q(k, 0.5) / 1000).toFixed(2)} ms  p99 ${(q(k, 0.99) / 1000).toFixed(2)} ms`;
console.log(`send-ready bench, ${n} warm runs (first 50 discarded)`);
for (const k of ['total', 'screen', 'state', 'build', 'sign'] as const) console.log(fmt(k));
