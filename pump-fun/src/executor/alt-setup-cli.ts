import {
  AddressLookupTableProgram,
  ComputeBudgetProgram,
  Connection,
  PublicKey,
  Transaction,
  type TransactionInstruction,
} from '@solana/web3.js';
import { loadConfig, ConfigError } from '../config/load.ts';
import { PROGRAM_IDS, WSOL_MINT } from '../core/constants.ts';
import { RpcClient } from '../core/rpc.ts';
import { decodeMint } from '../enrichment/mint.ts';
import { fetchPumpSwapPool } from '../enrichment/pool.ts';
import { PumpAmmClient } from './pumpAmm.ts';
import { createIdempotentAtaInstruction, DEFAULT_PROBE_SLIPPAGE_PCT, PROBE_SOL } from './sellability.ts';
import { selectAltMembers } from './altMembers.ts';
import { Wallet } from './wallet.ts';
import { assembleSignedSwapTx } from './assemble.ts';

/**
 * H4 sellability ALT setup (one-time, OPERATOR-APPROVED).
 *
 * The atomic buy+sell probe (1255–1287 bytes) overflows Solana's 1232-byte tx
 * limit; the probe already supports an address lookup table
 * (guardrails.sellabilityLookupTableAddress) but none exists yet.
 *
 *   npm run alt:setup -- --mint <MINT>              # dry run: print inventory + projected size (sends NOTHING)
 *   npm run alt:setup -- --mint <MINT> --execute    # create + extend the ALT (sends 2 txs, ~0.002 SOL rent)
 *
 * After --execute prints the ALT address:
 *   1. Set guardrails.sellabilityLookupTableAddress: <ALT> in config.yaml.
 *   2. Restart (no hot-reload).
 *   3. Confirm H4 "atomic buy+sell simulated cleanly" becomes the majority and
 *      logs show usedLookupTable: true with tx bytes < 1232.
 *
 * Member selection: every unique address from a real assembled probe EXCEPT
 * the per-mint/per-user ones (pool, vaults, base mint, user, user ATA), plus
 * the well-known static programs. Over-inclusion is harmless — the probe only
 * loads entries that match — while under-inclusion just leaves bytes on the
 * table. Each LUT-hit address shrinks from 32 bytes to a 1-byte index.
 */

const ALWAYS_STATIC = [
  PROGRAM_IDS.PUMP_SWAP,
  PROGRAM_IDS.PUMP_FEE,
  PROGRAM_IDS.TOKEN,
  PROGRAM_IDS.TOKEN_2022,
  PROGRAM_IDS.ASSOCIATED_TOKEN,
  PROGRAM_IDS.SYSTEM,
  PROGRAM_IDS.COMPUTE_BUDGET,
  WSOL_MINT,
];

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

async function main(): Promise<void> {
  const mint = arg('--mint');
  if (!mint) throw new ConfigError('pass --mint <MINT> of a recent graduation pool');
  const execute = process.argv.includes('--execute');

  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  if (!config.rpc?.primaryHttp) throw new ConfigError('rpc.primaryHttp is required');
  const wallet = Wallet.load(config.wallet.keypairEnvVar, 'live');
  const user = wallet.keypair.publicKey;
  const rpc = new RpcClient({
    httpUrl: config.rpc.primaryHttp,
    fallbackHttpUrls: config.rpc.fallbackHttp,
    maxConcurrent: config.rpc.maxConcurrentRequests,
    timeoutMs: config.rpc.readTimeoutMs,
  });
  const connection = new Connection(config.rpc.primaryHttp, 'processed');

  const pool = await fetchPumpSwapPool(rpc, mint);
  if (!pool) throw new ConfigError(`no PumpSwap pool found for mint ${mint}`);
  const mintAcct = await rpc.getAccountInfoBase64(mint);
  if (!mintAcct) throw new ConfigError(`mint account not found: ${mint}`);
  const mintInfo = decodeMint(mintAcct.data, mintAcct.owner);

  const pumpAmm = new PumpAmmClient(config.rpc.primaryHttp, 'processed', {
    fallbackHttpUrls: config.rpc.fallbackHttp,
    timeoutMs: config.rpc.readTimeoutMs,
  });
  const probeLamports = BigInt(Math.floor(PROBE_SOL * 1e9));
  // Same builder the live probe uses (one state read, sell = exact buy output).
  const { buyIxs, sellIxs } = await pumpAmm.buildProbeSwap(
    pool.poolAddress,
    user,
    probeLamports,
    config.guardrails.sellabilityProbeSlippagePct ?? DEFAULT_PROBE_SLIPPAGE_PCT,
  );
  const tokenProgram = new PublicKey(mintInfo.isToken2022 ? PROGRAM_IDS.TOKEN_2022 : PROGRAM_IDS.TOKEN);
  const ataSetup = createIdempotentAtaInstruction(user, user, new PublicKey(mint), tokenProgram);
  const ataExists = Boolean(await connection.getAccountInfo(ataSetup.address, 'processed'));
  const setupIxs = ataExists ? [] : [ataSetup.instruction];

  const perProbe = new Set(
    [pool.poolAddress, pool.baseVault, pool.quoteVault, mint, user.toBase58(), ataSetup.address.toBase58()],
  );
  const memberList = selectAltMembers([...setupIxs, ...buyIxs, ...sellIxs], perProbe, ALWAYS_STATIC);

  // Size check with the same assembler the probe uses (no LUT yet).
  const probeBytes = await assembleSignedSwapTx([...setupIxs, ...buyIxs, ...sellIxs], {
    connection,
    wallet,
    feePlan: { priorityMicroLamports: 50_000, jitoTipLamports: 0 },
    computeUnitLimit: 600_000,
  });
  // Each member the probe references through the LUT saves 31 bytes.
  const referenced = new Set<string>();
  for (const ix of [...setupIxs, ...buyIxs, ...sellIxs]) {
    referenced.add(ix.programId.toBase58());
    for (const k of ix.keys) referenced.add(k.pubkey.toBase58());
  }
  const hits = memberList.filter((m) => referenced.has(m)).length;

  // eslint-disable-next-line no-console
  console.log([
    `pool ${pool.poolAddress}  baseVault ${pool.baseVault}  quoteVault ${pool.quoteVault}`,
    `probe instructions: setup ${setupIxs.length} + buy ${buyIxs.length} + sell ${sellIxs.length}`,
    `assembled probe: ${probeBytes.length} bytes (limit 1232)`,
    `ALT members: ${memberList.length} (${hits} referenced by this probe → ~${hits * 31} bytes saved)`,
    ...memberList.map((m) => `  ${m}`),
  ].join('\n'));

  if (!execute) {
    // eslint-disable-next-line no-console
    console.log('dry run — pass --execute to create + extend the ALT (2 transactions, ~0.002 SOL rent)');
    return;
  }
  const slot = await connection.getSlot('finalized');
  const [createIx, lutAddress] = AddressLookupTableProgram.createLookupTable({
    authority: user,
    payer: user,
    recentSlot: slot,
  });
  // eslint-disable-next-line no-console
  console.log(`creating ALT ${lutAddress.toBase58()} ...`);
  await send(connection, wallet, [createIx], config.fees.priorityFloorMicroLamports);
  // Extend in chunks so no single extend tx approaches the size limit.
  for (let i = 0; i < memberList.length; i += 20) {
    const chunk = memberList.slice(i, i + 20).map((m) => new PublicKey(m));
    const extendIx = AddressLookupTableProgram.extendLookupTable({
      payer: user,
      authority: user,
      lookupTable: lutAddress,
      addresses: chunk,
    });
    await send(connection, wallet, [extendIx], config.fees.priorityFloorMicroLamports);
    // eslint-disable-next-line no-console
    console.log(`  extended ${Math.min(i + 20, memberList.length)}/${memberList.length}`);
  }
  // eslint-disable-next-line no-console
  console.log(
    `ALT ready: ${lutAddress.toBase58()} — set guardrails.sellabilityLookupTableAddress to it in config.yaml and restart.`,
  );
}

async function send(
  connection: Connection,
  wallet: Wallet,
  ixs: TransactionInstruction[],
  priorityMicroLamports: number,
): Promise<void> {
  const tx = new Transaction().add(
    ComputeBudgetProgram.setComputeUnitPrice({ microLamports: priorityMicroLamports }),
    ...ixs,
  );
  tx.feePayer = wallet.keypair.publicKey;
  tx.recentBlockhash = (await connection.getLatestBlockhash('confirmed')).blockhash;
  tx.sign(wallet.keypair);
  const sig = await connection.sendRawTransaction(tx.serialize());
  await connection.confirmTransaction(sig, 'confirmed');
  // eslint-disable-next-line no-console
  console.log(`  tx ${sig}`);
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
