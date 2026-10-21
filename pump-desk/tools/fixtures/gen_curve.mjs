// Golden fixtures for pump-desk's hand-rolled bonding-curve transactions.
//
// Builds pump.fun buy/sell instructions with the official @pump-fun/pump-sdk
// (pinned in this directory's package.json), compiles them with web3.js
// MessageV0 exactly as pump-desk does (CU limit, CU price, instructions, tip),
// and writes the expected bytes next to the raw accounts they came from. The
// C++ tests decode the same accounts, build the same transactions and assert
// byte equality; quote vectors pin the curve maths to the SDK's.
//
//   (cd pump-desk/tools/fixtures && npm install --ignore-scripts)
//   node pump-desk/tools/fixtures/gen_curve.mjs [RPC_URL] [LIVE_CURVE_MINT]
//
// Reads a public RPC (default api.mainnet-beta.solana.com). LIVE_CURVE_MINT must
// be a pump.fun mint whose curve is still incomplete when this runs.
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(resolve(here, 'package.json'));
const web3 = require('@solana/web3.js');
const BN = require('bn.js');
const spl = require('@solana/spl-token');
const sdkPkg = require('@pump-fun/pump-sdk');

const { PublicKey, Connection, Keypair, MessageV0, ComputeBudgetProgram, SystemProgram } = web3;
const rpcUrl = process.argv[2] ?? 'https://api.mainnet-beta.solana.com';
const LIVE_MINT = new PublicKey(process.argv[3] ?? '5L4WmQY8eF1riggRFQ9Y4FGojyejsK8gfGemLFw8pump');
const conn = new Connection(rpcUrl, 'confirmed');

// The same wallet, tip and blockhash as gen.mjs, so both fixtures share one test user.
const TIP = new PublicKey('4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE');
const BLOCKHASH = 'GHtXQBsoZHVnNFa9YevAzFr17DJjgHXk3ycTKD5xD3Zi';
// The migrated seed mint of gen.mjs: its curve is complete (the boot self-check's target).
const SEED_MINT = new PublicKey('74xrbNsxV8uPT6Kcedk2T3FNPtw8jzyVNWmaVkzxpump');
const seed = Uint8Array.from({ length: 32 }, (_, i) => i + 1);
const user = Keypair.fromSeed(seed).publicKey;
const sdk = sdkPkg.PUMP_SDK;

const [globalAcc, feeCfgAcc, curveAcc, mintAcc, seedCurveAcc] = await conn.getMultipleAccountsInfo([
  sdkPkg.GLOBAL_PDA,
  sdkPkg.PUMP_FEE_CONFIG_PDA,
  sdkPkg.bondingCurvePda(LIVE_MINT),
  LIVE_MINT,
  sdkPkg.bondingCurvePda(SEED_MINT),
]);
if (!globalAcc || !feeCfgAcc || !curveAcc || !mintAcc || !seedCurveAcc) throw new Error('account read failed');
const global = sdk.decodeGlobal(globalAcc);
const feeConfig = sdk.decodeFeeConfig(feeCfgAcc);
const curve = sdk.decodeBondingCurve(curveAcc);
if (curve.complete) throw new Error(`${LIVE_MINT.toBase58()} is complete: pass a live curve's mint`);
const tokenProgram = mintAcc.owner;
const mintSupply = new BN(spl.unpackMint(LIVE_MINT, mintAcc, tokenProgram).supply.toString());

const hex = (b) => Buffer.from(b).toString('hex');
const ixJson = (ix) => ({
  program: ix.programId.toBase58(),
  keys: ix.keys.map((k) => ({ pubkey: k.pubkey.toBase58(), signer: k.isSigner, writable: k.isWritable })),
  data: hex(ix.data),
});
const compile = (ixs, params) => {
  const all = [
    ComputeBudgetProgram.setComputeUnitLimit({ units: params.cuLimit }),
    ComputeBudgetProgram.setComputeUnitPrice({ microLamports: params.cuPrice }),
    ...ixs,
    SystemProgram.transfer({ fromPubkey: user, toPubkey: TIP, lamports: params.tip }),
  ];
  return hex(MessageV0.compile({ payerKey: user, instructions: all, recentBlockhash: BLOCKHASH }).serialize());
};

// Index 0 of each recipient list, which is what the C++ picks for fee_recipient_index / buyback_index 0.
const feeRecipient = (mayhem) => (mayhem ? global.reservedFeeRecipient : global.feeRecipient);
const buyback = global.buybackFeeRecipients[0];
const userAta = spl.getAssociatedTokenAddressSync(LIVE_MINT, user, true, tokenProgram);

async function buyIxs(v) {
  const common = {
    feeRecipient: feeRecipient(v.mayhem),
    mint: LIVE_MINT,
    associatedUser: userAta,
    user,
    creatorVault: sdkPkg.creatorVaultPda(curve.creator),
    tokenProgram,
  };
  const remaining = [
    { pubkey: sdkPkg.bondingCurveV2Pda(LIVE_MINT), isWritable: false, isSigner: false },
    { pubkey: buyback, isWritable: true, isSigner: false },
  ];
  // partial_fill=false is the SDK's own builder; partial_fill=true is the same call with the flag set.
  const ix = v.partial
    ? await sdk.offlinePumpProgram.methods.buy(new BN(v.amount), new BN(v.maxSol), { 0: true }, { 0: true })
        .accountsPartial(common).remainingAccounts(remaining).instruction()
    : await sdk.getBuyInstructionRaw({ user, mint: LIVE_MINT, creator: curve.creator, amount: new BN(v.amount),
        solAmount: new BN(v.maxSol), feeRecipient: common.feeRecipient, tokenProgram, buybackFeeRecipient: buyback });
  return [spl.createAssociatedTokenAccountIdempotentInstruction(user, userAta, user, LIVE_MINT, tokenProgram), ix];
}

async function sellIxs(v) {
  const ix = await sdk.getSellInstructionRaw({ user, mint: LIVE_MINT, creator: curve.creator, amount: new BN(v.amount),
    solAmount: new BN(v.minSol), feeRecipient: feeRecipient(v.mayhem), buybackFeeRecipient: buyback, tokenProgram,
    cashback: !!v.cashback });
  return [ix, spl.createCloseAccountInstruction(userAta, user, user, [], tokenProgram)];
}

const variants = [
  { name: 'buy-partial', side: 'buy', amount: '326290188808', maxSol: '13000000', partial: true },
  { name: 'buy-exact', side: 'buy', amount: '1234567890', maxSol: '50000', partial: false },
  { name: 'buy-mayhem', side: 'buy', amount: '326290188808', maxSol: '13000000', partial: true, mayhem: true },
  { name: 'sell-plain', side: 'sell', amount: '326290188808', minSol: '9000000' },
  { name: 'sell-cashback', side: 'sell', amount: '777777777', minSol: '1', cashback: true },
];
const params = { cuLimit: 150000, cuPrice: 1234567, tip: 5000 };
const cases = [];
for (const v of variants) {
  const ixs = v.side === 'buy' ? await buyIxs(v) : await sellIxs(v);
  cases.push({ ...v, ixs: ixs.map(ixJson), message: compile(ixs, params) });
}

// Quote vectors against the SDK's maths on the live curve state.
const fees = sdkPkg.computeFeesBps({ global, feeConfig, mintSupply: curve.isMayhemMode ? mintSupply : new BN('1000000000000000'),
  virtualQuoteReserves: curve.virtualQuoteReserves, virtualTokenReserves: curve.virtualTokenReserves,
  quoteMint: curve.quoteMint, creatorFeeBps: curve.creatorFeeBps });
const quotes = [];
for (const lamports of ['10000000', '50000000', '123456789', '1000000000']) {
  const tokens = sdkPkg.getBuyTokenAmountFromSolAmount({ global, feeConfig, mintSupply, bondingCurve: curve, amount: new BN(lamports) });
  const cost = sdkPkg.getBuySolAmountFromTokenAmount({ global, feeConfig, mintSupply, bondingCurve: curve, amount: tokens });
  const proceeds = sdkPkg.getSellSolAmountFromTokenAmount({ global, feeConfig, mintSupply, bondingCurve: curve, amount: tokens });
  quotes.push({ lamports, tokens: tokens.toString(), cost: cost.toString(), proceeds: proceeds.toString() });
}

const out = {
  generated: new Date().toISOString(),
  sdkVersion: JSON.parse(readFileSync(resolve(here, 'node_modules/@pump-fun/pump-sdk/package.json'), 'utf8')).version,
  liveMint: LIVE_MINT.toBase58(),
  seedMint: SEED_MINT.toBase58(),
  tip: TIP.toBase58(),
  blockhash: BLOCKHASH,
  userSeedHex: hex(seed),
  params,
  accounts: {
    pumpGlobal: globalAcc.data.toString('base64'),
    curve: curveAcc.data.toString('base64'),
    seedCurve: seedCurveAcc.data.toString('base64'),
    mintOwner: tokenProgram.toBase58(),
  },
  curve: {
    creator: curve.creator.toBase58(),
    complete: curve.complete,
    mayhem: curve.isMayhemMode,
    cashback: curve.isCashbackCoin,
    creatorFeeBps: curve.creatorFeeBps?.toString() ?? '0',
  },
  fees: { protocolFeeBps: fees.protocolFeeBps.toString(), creatorFeeBps: fees.creatorFeeBps.toString() },
  pdas: {
    global: sdkPkg.GLOBAL_PDA.toBase58(),
    eventAuthority: sdkPkg.PUMP_EVENT_AUTHORITY_PDA.toBase58(),
    globalVolumeAccumulator: sdkPkg.GLOBAL_VOLUME_ACCUMULATOR_PDA.toBase58(),
    feeConfig: sdkPkg.PUMP_FEE_CONFIG_PDA.toBase58(),
    bondingCurve: sdkPkg.bondingCurvePda(LIVE_MINT).toBase58(),
    bondingCurveV2: sdkPkg.bondingCurveV2Pda(LIVE_MINT).toBase58(),
    creatorVault: sdkPkg.creatorVaultPda(curve.creator).toBase58(),
    userVolumeAccumulator: sdkPkg.userVolumeAccumulatorPda(user).toBase58(),
    userAta: userAta.toBase58(),
  },
  recipients: {
    fee: global.feeRecipient.toBase58(),
    reserved: global.reservedFeeRecipient.toBase58(),
    buyback: buyback.toBase58(),
  },
  quotes,
  cases,
};
const path = resolve(here, '../../tests/fixtures/curve_golden.json');
writeFileSync(path, JSON.stringify(out, null, 1));
console.log(`wrote ${path}: ${cases.length} cases, ${quotes.length} quotes, mint owner ${tokenProgram.toBase58()}, fees ${fees.protocolFeeBps}+${fees.creatorFeeBps} bps`);
