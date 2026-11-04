// Golden v0 transactions from @solana/web3.js 1.99 (already on disk under pump-desk's fixture tools):
//   NODE_PATH=../../../pump-desk/tools/fixtures/node_modules node gen.mjs > golden.json
// Every instruction is encoded here independently of the C++ builders (web3.js's own ComputeBudget,
// System and AddressLookupTable helpers; Kamino flash and executor data by their documented layouts).
// The C++ test rebuilds the same transactions with src/tx and requires byte-identical messages and
// signatures (ed25519 is deterministic; the payer is Keypair.fromSeed of a fixed test seed).
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const web3 = require('@solana/web3.js');
const {
  PublicKey, Keypair, TransactionMessage, VersionedTransaction, ComputeBudgetProgram, SystemProgram,
  AddressLookupTableProgram, AddressLookupTableAccount, TransactionInstruction,
} = web3;

const seed = Uint8Array.from({ length: 32 }, (_, i) => i + 1);
const payer = Keypair.fromSeed(seed);
const key = (n) => {
  const b = new Uint8Array(32);
  b[0] = n; b[1] = 0x42; b[31] = 0x99;
  return new PublicKey(b);
};
const KLEND = new PublicKey('KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD');
const TOKEN = new PublicKey('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA');
const SYSVAR_IX = new PublicKey('Sysvar1nstructions1111111111111111111111111');
const blockhash = new PublicKey(Uint8Array.from({ length: 32 }, (_, i) => 200 - i)).toBase58();

const u64 = (v) => { const b = Buffer.alloc(8); b.writeBigUInt64LE(BigInt(v)); return b; };
const DISC_BORROW = Buffer.from([135, 231, 52, 167, 7, 52, 212, 193]);
const DISC_REPAY = Buffer.from([185, 117, 0, 203, 96, 245, 180, 186]);

// Kamino reserve accounts (synthetic keys; the C++ side derives the market authority the same way).
const reserve = { reserve: key(10), market: key(11), mint: key(12), supply: key(13), fee: key(14) };
const [lma] = PublicKey.findProgramAddressSync([Buffer.from('lma'), reserve.market.toBuffer()], KLEND);
const userAta = key(20);
const flashKeys = () => [
  { pubkey: payer.publicKey, isSigner: true, isWritable: false },
  { pubkey: lma, isSigner: false, isWritable: false },
  { pubkey: reserve.market, isSigner: false, isWritable: false },
  { pubkey: reserve.reserve, isSigner: false, isWritable: true },
  { pubkey: reserve.mint, isSigner: false, isWritable: false },
  { pubkey: reserve.supply, isSigner: false, isWritable: true },
  { pubkey: userAta, isSigner: false, isWritable: true },
  { pubkey: reserve.fee, isSigner: false, isWritable: true },
  { pubkey: KLEND, isSigner: false, isWritable: false },
  { pubkey: KLEND, isSigner: false, isWritable: false },
  { pubkey: SYSVAR_IX, isSigner: false, isWritable: false },
  { pubkey: TOKEN, isSigner: false, isWritable: false },
];
const amount = 123_456_789_000n;
const borrow = new TransactionInstruction({ programId: KLEND, keys: flashKeys(), data: Buffer.concat([DISC_BORROW, u64(amount)]) });
const repayAt = (idx) => new TransactionInstruction({ programId: KLEND, keys: flashKeys(), data: Buffer.concat([DISC_REPAY, u64(amount), Buffer.from([idx])]) });

// Executor: tag 0 | amount_in | min_out | num_legs | 3 x (dex, a_to_b, acct_start, acct_len)
const executor = key(30);
const execute = (legAccounts) => {
  const keys = [{ pubkey: payer.publicKey, isSigner: true, isWritable: false }, { pubkey: userAta, isSigner: false, isWritable: true },
    { pubkey: key(21), isSigner: false, isWritable: true }];
  for (let i = 0; i < legAccounts; i++) keys.push({ pubkey: key(40 + i), isSigner: false, isWritable: i % 3 !== 0 });
  keys.push({ pubkey: payer.publicKey, isSigner: true, isWritable: false });  // a leg's user authority (dedups)
  const half = Math.floor((legAccounts + 1) / 2);
  const data = Buffer.concat([Buffer.from([0]), u64(amount), u64(amount + 5_000_000n),
    Buffer.from([2, 0, 1, 0, half, 1, 0, half, legAccounts + 1 - half, 0, 0, 0, 0])]);
  return new TransactionInstruction({ programId: executor, keys, data });
};
const tipAccount = new PublicKey('96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5');

const route = (withPrice, legAccounts = 18) => {
  const ixs = [ComputeBudgetProgram.setComputeUnitLimit({ units: 412_345 })];
  if (withPrice) ixs.push(ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 7_777 }));
  const bi = ixs.length;
  ixs.push(borrow, execute(legAccounts), repayAt(bi), SystemProgram.transfer({ fromPubkey: payer.publicKey, toPubkey: tipAccount, lamports: 1_234_567 }));
  return ixs;
};

// Lookup tables: the first holds programs and reserve accounts (and, deliberately,
// the payer, which as a signer must stay static; the Jito tip account is never put in a table); the second holds the leg accounts.
const t1 = new AddressLookupTableAccount({ key: key(90), state: { deactivationSlot: 2n ** 64n - 1n, lastExtendedSlot: 0, lastExtendedSlotStartIndex: 0,
  addresses: [KLEND, TOKEN, SYSVAR_IX, lma, reserve.market, reserve.reserve, reserve.mint, reserve.supply, reserve.fee, payer.publicKey, executor, userAta] } });
const t2 = new AddressLookupTableAccount({ key: key(91), state: { deactivationSlot: 2n ** 64n - 1n, lastExtendedSlot: 0, lastExtendedSlotStartIndex: 0,
  addresses: Array.from({ length: 18 }, (_, i) => key(40 + i)).concat([key(21)]) } });

const enc = (ix) => ({ program: ix.programId.toBase58(), accounts: ix.keys.map((k) => [k.pubkey.toBase58(), k.isSigner, k.isWritable]),
  data: Buffer.from(ix.data).toString('base64') });
const tbl = (t) => ({ key: t.key.toBase58(), addresses: t.state.addresses.map((a) => a.toBase58()) });

const cases = [];
const add = (name, ixs, tables) => {
  const msg = new TransactionMessage({ payerKey: payer.publicKey, recentBlockhash: blockhash, instructions: ixs }).compileToV0Message(tables);
  const tx = new VersionedTransaction(msg);
  tx.sign([payer]);
  cases.push({ name, instructions: ixs.map(enc), tables: tables.map(tbl), message: Buffer.from(msg.serialize()).toString('base64'),
    tx: Buffer.from(tx.serialize()).toString('base64') });
};
add('route_no_alt', route(false, 6), []);  // the full route does not fit without tables
add('route_one_alt', route(true), [t1]);
add('route_two_alts', route(false), [t1, t2]);
const [createIx, table] = AddressLookupTableProgram.createLookupTable({ authority: payer.publicKey, payer: payer.publicKey, recentSlot: 271_828_182 });
const extendIx = AddressLookupTableProgram.extendLookupTable({ lookupTable: table, authority: payer.publicKey, payer: payer.publicKey,
  addresses: Array.from({ length: 20 }, (_, i) => key(100 + i)) });
add('alt_create_extend', [createIx, extendIx], []);

console.log(JSON.stringify({
  generator: `@solana/web3.js ${require('@solana/web3.js/package.json').version}`, seed: Buffer.from(seed).toString('hex'),
  payer: payer.publicKey.toBase58(), blockhash, reserve: Object.fromEntries(Object.entries(reserve).map(([k, v]) => [k, v.toBase58()])),
  lma: lma.toBase58(), user_ata: userAta.toBase58(), tip_account: tipAccount.toBase58(), executor: executor.toBase58(),
  amount: amount.toString(), alt_table: table.toBase58(), cases,
}, null, 1));
