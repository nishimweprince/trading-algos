// Golden fixtures for pump-desk's hand-rolled transaction bytes.
//
// Builds PumpSwap buy/sell instructions with the official
// @pump-fun/pump-swap-sdk (from the pump-fun checkout's node_modules), compiles
// them with web3.js MessageV0 exactly as pump-desk does (CU limit, CU price,
// SDK instructions, tip), and writes the expected bytes next to the raw
// account data they came from. The C++ tests decode the same accounts, build
// the same transactions and assert byte equality.
//
//   node pump-desk/tools/fixtures/gen.mjs [RPC_URL]
//
// Reads 4 accounts from a public RPC (default api.mainnet-beta.solana.com).
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(resolve(here, '../../../pump-fun/package.json'));
const web3 = require('@solana/web3.js');
const BN = require('bn.js');
const sdk = require('@pump-fun/pump-swap-sdk');
const spl = require('@solana/spl-token');

const { PublicKey, Connection, Keypair, MessageV0, ComputeBudgetProgram, SystemProgram } = web3;
const rpcUrl = process.argv[2] ?? 'https://api.mainnet-beta.solana.com';
const conn = new Connection(rpcUrl, 'confirmed');

// pump-fun/src/racer/sendReady.ts BOOT_SEED_POOL
const SEED_MINT = new PublicKey('74xrbNsxV8uPT6Kcedk2T3FNPtw8jzyVNWmaVkzxpump');
const SEED_POOL = new PublicKey('28jtYEpTMpiTmgMUXjS7mgkoqpoqxnrHtay99xQoKHZn');
const TIP = new PublicKey('4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE');
const BLOCKHASH = 'GHtXQBsoZHVnNFa9YevAzFr17DJjgHXk3ycTKD5xD3Zi';
const seed = Uint8Array.from({ length: 32 }, (_, i) => i + 1);
const user = Keypair.fromSeed(seed).publicKey;

// The SDK picks fee recipients with Math.random(); pin index 0 for determinism.
Math.random = () => 0;

const [global, feeCfg, pool, mint] = await conn.getMultipleAccountsInfo([
  sdk.GLOBAL_CONFIG_PDA,
  sdk.PUMP_AMM_FEE_CONFIG_PDA,
  SEED_POOL,
  SEED_MINT,
]);
if (!global || !feeCfg || !pool || !mint) throw new Error('account read failed');

const globalConfig = sdk.PUMP_AMM_SDK.decodeGlobalConfig(global);
const feeConfig = sdk.PUMP_AMM_SDK.decodeFeeConfig(feeCfg);
const decodedPool = sdk.PUMP_AMM_SDK.decodePool(pool);
const baseTokenProgram = mint.owner;
const mintInfo = spl.unpackMint(SEED_MINT, mint, baseTokenProgram);
const offline = new sdk.PumpAmmSdk();

const hex = (b) => Buffer.from(b).toString('hex');
const ixJson = (ix) => ({
  program: ix.programId.toBase58(),
  keys: ix.keys.map((k) => ({ pubkey: k.pubkey.toBase58(), signer: k.isSigner, writable: k.isWritable })),
  data: hex(ix.data),
});

function state(variant) {
  const p = { ...decodedPool };
  if (variant.coinCreatorDefault) p.coinCreator = PublicKey.default;
  if (variant.cashback) p.isCashbackCoin = true;
  if (variant.mayhem) p.isMayhemMode = true;
  if (variant.creatorFeeBps !== undefined) p.creatorFeeBps = new BN(variant.creatorFeeBps);
  p.virtualQuoteReserves = new BN(variant.virtualQuote);
  const userBaseAta = spl.getAssociatedTokenAddressSync(SEED_MINT, user, true, baseTokenProgram);
  const userQuoteAta = spl.getAssociatedTokenAddressSync(spl.NATIVE_MINT, user, true, spl.TOKEN_PROGRAM_ID);
  return {
    state: {
      globalConfig,
      feeConfig,
      poolKey: SEED_POOL,
      poolAccountInfo: pool,
      pool: p,
      poolBaseAmount: new BN(variant.baseReserve),
      poolQuoteAmount: new BN(variant.quoteReserve),
      baseTokenProgram,
      quoteTokenProgram: spl.TOKEN_PROGRAM_ID,
      baseMint: SEED_MINT,
      baseMintAccount: mintInfo,
      user,
      userBaseTokenAccount: userBaseAta,
      userQuoteTokenAccount: userQuoteAta,
      // buy: base ATA missing (created); sell: base ATA present. WSOL ATA is always missing.
      userBaseAccountInfo: variant.side === 'sell' ? { owner: baseTokenProgram, data: Buffer.alloc(165), lamports: 1 } : null,
      userQuoteAccountInfo: null,
    },
    userBaseAta,
    userQuoteAta,
  };
}

function compile(ixs, params) {
  const all = [
    ComputeBudgetProgram.setComputeUnitLimit({ units: params.cuLimit }),
    ComputeBudgetProgram.setComputeUnitPrice({ microLamports: params.cuPrice }),
    ...ixs,
    SystemProgram.transfer({ fromPubkey: user, toPubkey: TIP, lamports: params.tip }),
  ];
  const msg = MessageV0.compile({ payerKey: user, instructions: all, recentBlockhash: BLOCKHASH });
  return hex(msg.serialize());
}

const base = { baseReserve: '206900000000000', quoteReserve: '67410000000', virtualQuote: '17580000000' };
const variants = [
  { name: 'buy-plain', side: 'buy', amount: '30000000', slippage: 30, ...base },
  { name: 'buy-small-slip', side: 'buy', amount: '123456789', slippage: 5, ...base, baseReserve: '150000000000000', quoteReserve: '90000000000' },
  { name: 'buy-no-coin-creator', side: 'buy', amount: '30000000', slippage: 30, coinCreatorDefault: true, ...base },
  { name: 'buy-cashback', side: 'buy', amount: '50000000', slippage: 10, cashback: true, ...base },
  { name: 'buy-mayhem', side: 'buy', amount: '40000000', slippage: 20, mayhem: true, ...base },
  { name: 'buy-creator-fee', side: 'buy', amount: '40000000', slippage: 20, creatorFeeBps: 77, ...base },
  { name: 'buy-no-virtual', side: 'buy', amount: '40000000', slippage: 20, ...base, virtualQuote: '0' },
  { name: 'sell-plain', side: 'sell', amount: '1234567890123', slippage: 20, ...base },
  { name: 'sell-cashback', side: 'sell', amount: '987654321', slippage: 90, cashback: true, ...base },
  { name: 'sell-no-coin-creator', side: 'sell', amount: '5555555555', slippage: 8, coinCreatorDefault: true, ...base },
];

const params = { cuLimit: 250000, cuPrice: 1234567, tip: 5000 };
const cases = [];
for (const v of variants) {
  const { state: st } = state(v);
  const ixs =
    v.side === 'buy'
      ? await offline.buyQuoteInput(st, new BN(v.amount), v.slippage)
      : await offline.sellBaseInput(st, new BN(v.amount), v.slippage);
  cases.push({ ...v, ixs: ixs.map(ixJson), message: compile(ixs, params) });
}

const pdas = {
  user: user.toBase58(),
  userWsolAta: spl.getAssociatedTokenAddressSync(spl.NATIVE_MINT, user, true).toBase58(),
  userVolumeAcc: sdk.userVolumeAccumulatorPda(user).toBase58(),
  globalConfig: sdk.GLOBAL_CONFIG_PDA.toBase58(),
  feeConfig: sdk.PUMP_AMM_FEE_CONFIG_PDA.toBase58(),
  canonicalPool: sdk.canonicalPumpPoolPda(SEED_MINT).toBase58(),
  poolAuthority: sdk.pumpPoolAuthorityPda(SEED_MINT).toBase58(),
  coinCreatorVaultAuthority: sdk.coinCreatorVaultAuthorityPda(decodedPool.coinCreator).toBase58(),
};

// Extra PDA/ATA vectors: many random owners/mints exercise the on-curve check.
const pdaVectors = [];
for (let i = 0; i < 64; i++) {
  const owner = Keypair.fromSeed(Uint8Array.from({ length: 32 }, (_, j) => (i * 7 + j * 13) & 0xff)).publicKey;
  const m = Keypair.fromSeed(Uint8Array.from({ length: 32 }, (_, j) => (i * 11 + j * 3 + 1) & 0xff)).publicKey;
  pdaVectors.push({
    owner: owner.toBase58(),
    mint: m.toBase58(),
    ataToken: spl.getAssociatedTokenAddressSync(m, owner, true, spl.TOKEN_PROGRAM_ID).toBase58(),
    ata2022: spl.getAssociatedTokenAddressSync(m, owner, true, spl.TOKEN_2022_PROGRAM_ID).toBase58(),
    canonicalPool: sdk.canonicalPumpPoolPda(m).toBase58(),
    bondingCurve: PublicKey.findProgramAddressSync([Buffer.from('bonding-curve'), m.toBuffer()], new PublicKey('6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'))[0].toBase58(),
  });
}

// Raw on-curve vectors (32-byte strings, both outcomes).
const curveVectors = [];
for (let i = 0; i < 256; i++) {
  const b = Buffer.alloc(32);
  for (let j = 0; j < 32; j++) b[j] = (i * 31 + j * 17 + ((i * j) & 0x5a)) & 0xff;
  curveVectors.push({ bytes: hex(b), onCurve: PublicKey.isOnCurve(b) });
}

const out = {
  generated: new Date().toISOString(),
  sdkVersion: JSON.parse(require('node:fs').readFileSync(resolve(here, '../../../pump-fun/node_modules/@pump-fun/pump-swap-sdk/package.json'), 'utf8')).version,
  seedMint: SEED_MINT.toBase58(),
  seedPool: SEED_POOL.toBase58(),
  tip: TIP.toBase58(),
  blockhash: BLOCKHASH,
  userSeedHex: hex(seed),
  params,
  accounts: {
    globalConfig: global.data.toString('base64'),
    feeConfig: feeCfg.data.toString('base64'),
    pool: pool.data.toString('base64'),
    mint: mint.data.toString('base64'),
    mintOwner: baseTokenProgram.toBase58(),
  },
  pool: {
    coinCreator: decodedPool.coinCreator.toBase58(),
    creator: decodedPool.creator.toBase58(),
    dataLen: pool.data.length,
    virtualQuote: decodedPool.virtualQuoteReserves?.toString() ?? '0',
  },
  pdas,
  pdaVectors,
  curveVectors,
  cases,
};
const path = resolve(here, '../../tests/fixtures/golden.json');
writeFileSync(path, JSON.stringify(out, null, 1));
console.log(`wrote ${path}: ${cases.length} cases, mint owner ${baseTokenProgram.toBase58()}, pool ${pool.data.length} bytes`);
