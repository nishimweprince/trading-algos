// Fixture fetcher for jito-arb's offline tests (read-only: RPC reads only).
//
//   NODE_PATH=../../pump-desk/tools/fixtures/node_modules node tools/fetch_fixtures.mjs [RPC_URL]
//
// Pool lists come from the official Raydium and Orca pool APIs (top TVL pools that
// contain SOL or USDC). Account bytes come from getMultipleAccounts at one context
// slot per batch. Independent decodes for the decoder tests:
//   - token accounts and mints: @solana/spl-token unpackAccount/unpackMint (+ transfer-fee extension)
//   - Whirlpool and TickArray: @coral-xyz/anchor BorshAccountsCoder over harness/idl/whirlpool.json
//   - Raydium AMM v4: the Raydium pool-keys API (vaults, mints, open orders, target orders, market)
// Also records two small real getProgramAccounts responses for the discovery test.
// Writes tests/fixtures/{accounts,expected_decode,pools,gpa_raydium,gpa_whirlpool}.json.
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const web3 = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const anchor = require('@coral-xyz/anchor');
const { PublicKey } = web3;

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const out = (name) => resolve(root, 'tests/fixtures', name);
const RPC = process.argv[2] ?? (process.env.HELIUS_API_KEY
  ? `https://mainnet.helius-rpc.com/?api-key=${process.env.HELIUS_API_KEY}`
  : 'https://api.mainnet-beta.solana.com');

const RAYDIUM_V4 = new PublicKey('675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8');
const WHIRLPOOL = new PublicKey('whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc');
const KLEND = new PublicKey('KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD');
const WSOL = 'So11111111111111111111111111111111111111112';
const USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v';
const PYUSD = '2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo';  // Token-2022 with the transfer-fee extension
// Kamino Main Market reserves (SOL, USDC): checked below by decoding their liquidity mint.
const KAMINO_RESERVES = ['d4A2prbA2whesmvHaL88BH6Ewn5N4bTSU2Ze8P6Bc4Q', 'D6q6wuQSrifJKZYpR1M8R4YawnLDtDsMmWM1NbBmgJ59'];
const N_RAYDIUM = 50, N_WHIRLPOOL = 40;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let lastCall = 0;
async function rpc(method, params) {
  for (let attempt = 0; ; ++attempt) {
    const wait = lastCall + 250 - Date.now();  // <= 4 req/s
    if (wait > 0) await sleep(wait);
    lastCall = Date.now();
    const res = await fetch(RPC, { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) });
    if (res.status === 429 && attempt < 6) { await sleep(1000 * 2 ** attempt); continue; }
    const j = await res.json();
    if (j.error) throw new Error(`${method}: ${JSON.stringify(j.error)}`);
    return j.result;
  }
}
// The response body verbatim (JSON.parse would round u64 fields such as rentEpoch).
async function rpcRaw(method, params) {
  const wait = lastCall + 250 - Date.now();
  if (wait > 0) await sleep(wait);
  lastCall = Date.now();
  const res = await fetch(RPC, { method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) });
  const text = await res.text();
  if (!res.ok || JSON.parse(text).error) throw new Error(`${method}: ${text.slice(0, 200)}`);
  return text;
}
async function getJson(url) {
  await sleep(300);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  return res.json();
}

const accounts = {};  // pubkey -> {owner, lamports, data, slot}
async function fetchAccounts(keys) {
  const uniq = [...new Set(keys)].filter((k) => !(k in accounts));
  for (let i = 0; i < uniq.length; i += 100) {
    const batch = uniq.slice(i, i + 100);
    const r = await rpc('getMultipleAccounts', [batch, { encoding: 'base64', commitment: 'confirmed' }]);
    r.value.forEach((v, j) => {
      accounts[batch[j]] = v ? { owner: v.owner, lamports: v.lamports, data: v.data[0], slot: r.context.slot } : null;
    });
  }
}
const bytes = (k) => Buffer.from(accounts[k].data, 'base64');

// --- pool lists -------------------------------------------------------------
async function raydiumPools() {
  const ids = [];
  for (const m of [WSOL, USDC]) {
    for (let page = 1; page <= 2 && ids.length < N_RAYDIUM * 2; ++page) {
      const j = await getJson(`https://api-v3.raydium.io/pools/info/mint?mint1=${m}&poolType=standard&poolSortField=liquidity&sortType=desc&pageSize=50&page=${page}`);
      for (const p of j.data.data) if (p.programId === RAYDIUM_V4.toBase58()) ids.push(p.id);
    }
  }
  const uniq = [...new Set(ids)];
  // Interleave SOL and USDC pools so both bases are represented.
  return uniq.slice(0, N_RAYDIUM);
}
async function whirlpools() {
  const list = [];
  let next;
  for (let page = 0; page < 6 && list.length < N_WHIRLPOOL; ++page) {
    const j = await getJson(`https://api.orca.so/v2/solana/pools?sortBy=tvl&sortDirection=desc&size=100${next ? `&next=${next}` : ''}`);
    for (const p of j.data) {
      if (p.poolType !== 'whirlpool') continue;
      if (![p.tokenMintA, p.tokenMintB].some((m) => m === WSOL || m === USDC)) continue;
      list.push(p.address);
      if (list.length >= N_WHIRLPOOL) break;
    }
    next = j.meta?.cursor?.next;
    if (!next) break;
  }
  return list;
}

// --- Whirlpool helpers ------------------------------------------------------
const idl = JSON.parse(readFileSync(resolve(root, 'harness/idl/whirlpool.json'), 'utf8'));
const coder = new anchor.BorshAccountsCoder(idl);
const TICK_ARRAY_SIZE = 88;
const floorDiv = (a, b) => Math.floor(a / b);
function tickArrayPda(pool, start) {
  return PublicKey.findProgramAddressSync([Buffer.from('tick_array'), new PublicKey(pool).toBuffer(), Buffer.from(String(start))], WHIRLPOOL)[0].toBase58();
}

const big = (v) => (v === null || v === undefined ? null : v.toString());

async function main() {
  console.error(`rpc: ${RPC.replace(/api-key=.*/, 'api-key=…')}`);
  // Program IDs: executable and owned by the upgradeable loader.
  const programs = {};
  for (const p of [RAYDIUM_V4, WHIRLPOOL, KLEND]) {
    const r = await rpc('getAccountInfo', [p.toBase58(), { encoding: 'base64', dataSlice: { offset: 0, length: 0 } }]);
    programs[p.toBase58()] = { executable: r.value.executable, owner: r.value.owner, slot: r.context.slot };
  }

  const rayIds = await raydiumPools();
  const wpIds = await whirlpools();
  console.error(`raydium ${rayIds.length} whirlpool ${wpIds.length}`);
  await fetchAccounts([...rayIds, ...wpIds, ...KAMINO_RESERVES]);

  const expected = { token_accounts: {}, mints: {}, whirlpools: {}, tick_arrays: {}, raydium: {}, reserves: {} };
  const vaults = [], mints = new Set([PYUSD]);
  for (const id of rayIds) {
    const d = bytes(id);
    const coinVault = new PublicKey(d.subarray(336, 368)).toBase58();
    const pcVault = new PublicKey(d.subarray(368, 400)).toBase58();
    vaults.push(coinVault, pcVault);
    mints.add(new PublicKey(d.subarray(400, 432)).toBase58());
    mints.add(new PublicKey(d.subarray(432, 464)).toBase58());
  }
  const tickArrays = [];
  for (const id of wpIds) {
    const w = coder.decode('Whirlpool', bytes(id));
    vaults.push(w.token_vault_a.toBase58(), w.token_vault_b.toBase58());
    mints.add(w.token_mint_a.toBase58());
    mints.add(w.token_mint_b.toBase58());
    const ticksInArray = TICK_ARRAY_SIZE * w.tick_spacing;
    const base = floorDiv(w.tick_current_index, ticksInArray) * ticksInArray;
    for (const o of [-1, 0, 1]) tickArrays.push(tickArrayPda(id, base + o * ticksInArray));
    expected.whirlpools[id] = {
      whirlpools_config: w.whirlpools_config.toBase58(), tick_spacing: w.tick_spacing,
      fee_tier_index: Buffer.from(w.fee_tier_index_seed).readUInt16LE(0),
      fee_rate: w.fee_rate, protocol_fee_rate: w.protocol_fee_rate, liquidity: big(w.liquidity), sqrt_price: big(w.sqrt_price),
      tick_current_index: w.tick_current_index, protocol_fee_owed_a: big(w.protocol_fee_owed_a), protocol_fee_owed_b: big(w.protocol_fee_owed_b),
      token_mint_a: w.token_mint_a.toBase58(), token_vault_a: w.token_vault_a.toBase58(), fee_growth_global_a: big(w.fee_growth_global_a),
      token_mint_b: w.token_mint_b.toBase58(), token_vault_b: w.token_vault_b.toBase58(), fee_growth_global_b: big(w.fee_growth_global_b),
    };
  }
  await fetchAccounts([...vaults, ...mints, ...tickArrays]);

  // Mints of the vaults (Raydium pools may carry mints not in the pool list above).
  for (const v of vaults) {
    const a = accounts[v];
    if (!a) continue;
    const acc = spl.unpackAccount(new PublicKey(v), { ...a, data: bytes(v), owner: new PublicKey(a.owner) }, new PublicKey(a.owner));
    expected.token_accounts[v] = { mint: acc.mint.toBase58(), owner: acc.owner.toBase58(), amount: big(acc.amount),
      delegate: acc.delegate?.toBase58() ?? null, state: acc.isFrozen ? 2 : (acc.isInitialized ? 1 : 0),
      is_native: acc.isNative, close_authority: acc.closeAuthority?.toBase58() ?? null, token_program: a.owner };
  }
  for (const m of mints) {
    const a = accounts[m];
    if (!a) continue;
    const info = spl.unpackMint(new PublicKey(m), { ...a, data: bytes(m), owner: new PublicKey(a.owner) }, new PublicKey(a.owner));
    const e = { decimals: info.decimals, supply: big(info.supply), mint_authority: info.mintAuthority?.toBase58() ?? null,
      freeze_authority: info.freezeAuthority?.toBase58() ?? null, token_program: a.owner, extensions: [] };
    if (a.owner === spl.TOKEN_2022_PROGRAM_ID.toBase58()) {
      e.extensions = spl.getExtensionTypes(info.tlvData).map((t) => spl.ExtensionType[t]);
      const tf = spl.getTransferFeeConfig(info);
      if (tf) e.transfer_fee = {
        older: { epoch: big(tf.olderTransferFee.epoch), max: big(tf.olderTransferFee.maximumFee), bps: tf.olderTransferFee.transferFeeBasisPoints },
        newer: { epoch: big(tf.newerTransferFee.epoch), max: big(tf.newerTransferFee.maximumFee), bps: tf.newerTransferFee.transferFeeBasisPoints },
      };
    }
    expected.mints[m] = e;
  }
  for (const t of tickArrays) {
    const a = accounts[t];
    if (!a) continue;
    const d = bytes(t);
    const disc = [...d.subarray(0, 8)];
    const fixedDisc = idl.accounts.find((x) => x.name === 'TickArray').discriminator;
    if (disc.join() !== fixedDisc.join()) {
      // DynamicTickArray: ticks are a Borsh enum { Uninitialized, Initialized(DynamicTickData) }.
      const ta = coder.decode('DynamicTickArray', d);
      const data = (x) => x.Initialized?.[0] ?? x.initialized?.[0] ?? null;
      expected.tick_arrays[t] = { kind: 'dynamic', start_tick_index: ta.start_tick_index, whirlpool: ta.whirlpool.toBase58(),
        initialized: ta.ticks.map((x, i) => (data(x) ? i : -1)).filter((i) => i >= 0),
        liquidity_net: ta.ticks.map(data).filter((x) => x).map((x) => big(x.liquidity_net)) };
      continue;
    }
    const ta = coder.decode('TickArray', d);
    expected.tick_arrays[t] = { kind: 'fixed', start_tick_index: ta.start_tick_index, whirlpool: ta.whirlpool.toBase58(),
      initialized: ta.ticks.map((x, i) => (x.initialized ? i : -1)).filter((i) => i >= 0),
      liquidity_net: ta.ticks.filter((x) => x.initialized).map((x) => big(x.liquidity_net)) };
  }
  // Raydium: the official pool-keys API.
  for (let i = 0; i < rayIds.length; i += 20) {
    const j = await getJson(`https://api-v3.raydium.io/pools/key/ids?ids=${rayIds.slice(i, i + 20).join(',')}`);
    for (const k of j.data) {
      if (!k) continue;
      expected.raydium[k.id] = { coin_vault: k.vault.A, pc_vault: k.vault.B, coin_mint: k.mintA.address, pc_mint: k.mintB.address,
        lp_mint: k.mintLp.address, open_orders: k.openOrders, target_orders: k.targetOrders, market: k.marketId,
        market_program: k.marketProgramId, authority: k.authority };
    }
  }
  for (const r of KAMINO_RESERVES) {
    const d = bytes(r);
    expected.reserves[r] = { lending_market: new PublicKey(d.subarray(32, 64)).toBase58(),
      liquidity_mint: new PublicKey(d.subarray(128, 160)).toBase58(), supply_vault: new PublicKey(d.subarray(160, 192)).toBase58(),
      fee_vault: new PublicKey(d.subarray(192, 224)).toBase58() };
  }

  // Two small real getProgramAccounts responses (SOL/USDC pools) for the discovery test.
  const gpaRayRaw = await rpcRaw('getProgramAccounts', [RAYDIUM_V4.toBase58(), { encoding: 'base64', commitment: 'confirmed',
    filters: [{ dataSize: 752 }, { memcmp: { offset: 400, bytes: WSOL } }, { memcmp: { offset: 432, bytes: USDC } }] }]);
  const gpaWpRaw = await rpcRaw('getProgramAccounts', [WHIRLPOOL.toBase58(), { encoding: 'base64', commitment: 'confirmed',
    filters: [{ dataSize: 653 }, { memcmp: { offset: 101, bytes: WSOL } }, { memcmp: { offset: 181, bytes: USDC } }] }]);

  const gpaRay = JSON.parse(gpaRayRaw).result, gpaWp = JSON.parse(gpaWpRaw).result;
  // Vaults and mints of the gPA pools, so discovery can value them offline.
  const gpaDeps = [];
  for (const e of gpaRay) {
    const d = Buffer.from(e.account.data[0], 'base64');
    for (const o of [336, 368, 400, 432]) gpaDeps.push(new PublicKey(d.subarray(o, o + 32)).toBase58());
  }
  for (const e of gpaWp) {
    const d = Buffer.from(e.account.data[0], 'base64');
    for (const o of [101, 133, 181, 213]) gpaDeps.push(new PublicKey(d.subarray(o, o + 32)).toBase58());
  }
  await fetchAccounts(gpaDeps);

  const keep = Object.fromEntries(Object.entries(accounts).map(([k, v]) => [k, v]));
  writeFileSync(out('accounts.json'), JSON.stringify({ rpc: RPC.includes('helius') ? 'helius' : 'public', programs, accounts: keep }));
  writeFileSync(out('expected_decode.json'), JSON.stringify(expected, null, 1));
  writeFileSync(out('pools.json'), JSON.stringify({ raydium_v4: rayIds, whirlpool: wpIds, kamino_reserves: KAMINO_RESERVES,
    t22_transfer_fee_mint: PYUSD }, null, 1));
  writeFileSync(out('gpa_raydium.json'), gpaRayRaw);
  writeFileSync(out('gpa_whirlpool.json'), gpaWpRaw);
  console.error(`accounts ${Object.keys(accounts).length}, gpa raydium ${gpaRay.length}, whirlpool ${gpaWp.length}`);
}

main().catch((e) => { console.error(e); process.exit(1); });
