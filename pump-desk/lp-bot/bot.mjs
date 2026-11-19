// pd-lp: LIVE liquidity provider on fresh PumpSwap canonical pools (pump.fun migrations).
//
// Rule (from the 2026-11-19 replay on pd-collect data, 22 pools: +0.94% mean, -0.01% median; unproven live):
//   on a non-mayhem WSOL migration: ENTRY_DELAY_MS after the migrate, buy the token with half of SIZE_SOL, then
//   deposit token + SOL; withdraw everything after HOLD_MS, or earlier when the position's mark falls STOP_PCT
//   below its cost. The exit is one tx (withdraw + sell + close the LP account); leftover dust is swept after.
// Risk: KILL file stops new entries (exits still run), MAX_OPEN positions, DAILY_LOSS_SOL realized per UTC day
// latches entries off until the next UTC day, MIN_RESERVE_SOL is never spent. Open positions persist in
// STATE_PATH and are exited on restart. P&L per position = the wallet's SOL change over its own txs (fees, tips,
// rent included).
//
// Env: HELIUS_API_KEY, WALLET_PATH, DATA_DIR (+ the tunables below). Never logs the key or the wallet secret.
import fs from "node:fs";
import path from "node:path";
import BN from "bn.js";
import {
  ComputeBudgetProgram, Connection, Keypair, PublicKey, TransactionMessage, VersionedTransaction,
} from "@solana/web3.js";
import {
  createCloseAccountInstruction, getAssociatedTokenAddressSync, TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID,
} from "@solana/spl-token";
import { OnlinePumpAmmSdk, PumpAmmSdk } from "@pump-fun/pump-swap-sdk";

const env = (k, d) => (process.env[k] ?? d);
const SIZE_SOL = Number(env("SIZE_SOL", "0.25"));
const MAX_OPEN = Number(env("MAX_OPEN", "2"));
const ENTRY_DELAY_MS = Number(env("ENTRY_DELAY_MS", "5000"));
const HOLD_MS = Number(env("HOLD_MS", "300000"));
const STOP_PCT = Number(env("STOP_PCT", "8"));
const DAILY_LOSS_SOL = Number(env("DAILY_LOSS_SOL", "0.1"));
const MIN_RESERVE_SOL = Number(env("MIN_RESERVE_SOL", "0.03"));
const SLIPPAGE_PCT = Number(env("SLIPPAGE_PCT", "5"));
const EXIT_SLIPPAGE_PCT = Number(env("EXIT_SLIPPAGE_PCT", "15"));
const WITHDRAW_SLIPPAGE_PCT = Number(env("WITHDRAW_SLIPPAGE_PCT", "3")); // proportional withdraw: covers reserve drift only
const CU_PRICE = Number(env("CU_PRICE_MICROLAMPORTS", "100000"));
const CU_LIMIT = Number(env("CU_LIMIT", "400000"));
const DATA_DIR = env("DATA_DIR", "/var/lib/pd-lp");
const KILL_PATH = path.join(DATA_DIR, "KILL");
const STATE_PATH = path.join(DATA_DIR, "state.json");
const LOG_PATH = path.join(DATA_DIR, "positions.jsonl");

const KEY = process.env.HELIUS_API_KEY;
if (!KEY) throw new Error("HELIUS_API_KEY missing");
const conn = new Connection(`https://mainnet.helius-rpc.com/?api-key=${KEY}`, {
  commitment: "confirmed", wsEndpoint: `wss://mainnet.helius-rpc.com/?api-key=${KEY}`,
});
const wallet = Keypair.fromSecretKey(Uint8Array.from(JSON.parse(fs.readFileSync(env("WALLET_PATH"), "utf8"))));
const user = wallet.publicKey;
const online = new OnlinePumpAmmSdk(conn);
const sdk = new PumpAmmSdk();

const PUMP = new PublicKey("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P");
const PUMP_AMM = new PublicKey("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA");
const MIGRATION_AUTHORITY = new PublicKey("39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg");
const WSOL = new PublicKey("So11111111111111111111111111111111111111112");
const EVENT_CPI_TAG = Buffer.from("e445a52e51cb9a1d", "hex");
const MIGRATION_EVENT = Buffer.from("bde95db95c94ea94", "hex"); // sha256("event:CompletePumpAmmMigrationEvent")[:8]
const LAMPORTS = 1e9;

const log = (msg, extra = {}) => console.log(JSON.stringify({ t: new Date().toISOString(), msg, ...extra }));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const bs58 = (() => {
  const A = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  return {
    decode(s) {
      let n = 0n;
      for (const c of s) n = n * 58n + BigInt(A.indexOf(c));
      const bytes = [];
      while (n > 0n) { bytes.unshift(Number(n & 255n)); n >>= 8n; }
      for (const c of s) { if (c !== "1") break; bytes.unshift(0); }
      return Buffer.from(bytes);
    },
  };
})();

// ------------------------------------------------------------------ state
let state = { open: {}, day: "", realizedToday: 0, seen: [] };
try { state = { ...state, ...JSON.parse(fs.readFileSync(STATE_PATH, "utf8")) }; } catch {}
const save = () => { fs.writeFileSync(STATE_PATH + ".tmp", JSON.stringify(state)); fs.renameSync(STATE_PATH + ".tmp", STATE_PATH); };
const today = () => new Date().toISOString().slice(0, 10);
function rollDay() { if (state.day !== today()) { state.day = today(); state.realizedToday = 0; save(); } }
const seen = new Set(state.seen);

// ------------------------------------------------------------------ tx helpers
async function send(ixs, label) {
  const { blockhash, lastValidBlockHeight } = await conn.getLatestBlockhash("confirmed");
  const msg = new TransactionMessage({
    payerKey: user, recentBlockhash: blockhash,
    instructions: [
      ComputeBudgetProgram.setComputeUnitLimit({ units: CU_LIMIT }),
      ComputeBudgetProgram.setComputeUnitPrice({ microLamports: CU_PRICE }),
      ...ixs,
    ],
  }).compileToV0Message();
  const tx = new VersionedTransaction(msg);
  tx.sign([wallet]);
  const sig = await conn.sendRawTransaction(tx.serialize(), { skipPreflight: false, maxRetries: 3 });
  const res = await conn.confirmTransaction({ signature: sig, blockhash, lastValidBlockHeight }, "confirmed");
  if (res.value.err) throw new Error(`${label} failed on chain: ${JSON.stringify(res.value.err)} ${sig}`);
  return sig;
}

async function solDelta(sig) {
  // the wallet's lamport change in this tx (fees, tips, rent and swaps included)
  for (let i = 0; i < 10; i++) {
    const tx = await conn.getTransaction(sig, { commitment: "confirmed", maxSupportedTransactionVersion: 0 });
    if (tx?.meta) {
      const keys = tx.transaction.message.getAccountKeys({ accountKeysFromLookups: tx.meta.loadedAddresses });
      for (let k = 0; k < keys.length; k++) if (keys.get(k).equals(user)) return tx.meta.postBalances[k] - tx.meta.preBalances[k];
      return 0;
    }
    await sleep(500);
  }
  return null;
}

async function tokenBalance(ata) {
  try { return new BN((await conn.getTokenAccountBalance(ata, "confirmed")).value.amount); } catch { return new BN(0); }
}

// Poll until the balance is >= min (min > 0): right after a confirmation another RPC node may still show the
// old balance (2026-11-19 first live entry deposited 0 because the fresh token balance read as 0).
async function waitBalance(ata, min, timeoutMs = 10_000) {
  const end = Date.now() + timeoutMs;
  let b = await tokenBalance(ata);
  while (b.lt(min) && Date.now() < end) { await sleep(400); b = await tokenBalance(ata); }
  return b;
}

// ------------------------------------------------------------------ signal: migrations
async function migrationFromSig(sig) {
  for (let i = 0; i < 20; i++) {
    const tx = await conn.getTransaction(sig, { commitment: "confirmed", maxSupportedTransactionVersion: 0 }).catch(() => null);
    if (tx?.meta) {
      if (tx.meta.err) return null;
      const keys = tx.transaction.message.getAccountKeys({ accountKeysFromLookups: tx.meta.loadedAddresses });
      for (const group of tx.meta.innerInstructions ?? []) {
        for (const ix of group.instructions) {
          if (!keys.get(ix.programIdIndex)?.equals(PUMP)) continue;
          const d = bs58.decode(ix.data);
          if (d.length >= 176 && d.subarray(0, 8).equals(EVENT_CPI_TAG) && d.subarray(8, 16).equals(MIGRATION_EVENT)) {
            return { mint: new PublicKey(d.subarray(48, 80)), pool: new PublicKey(d.subarray(144, 176)) };
          }
        }
      }
      return null;
    }
    await sleep(300);
  }
  return null;
}

// ------------------------------------------------------------------ position lifecycle
function poolTokenProgramAtas(pool, baseTokenProgram) {
  return {
    base: getAssociatedTokenAddressSync(pool.baseMint, user, true, baseTokenProgram),
    lp: getAssociatedTokenAddressSync(pool.lpMint, user, true, TOKEN_2022_PROGRAM_ID),
  };
}

async function markValue(pos) {
  // SOL we would get now: our LP share of both vaults + leftover token, token sold into the pool (fee 1.25%)
  const pool = await online.fetchPool(new PublicKey(pos.pool));
  const [bv, qv] = await Promise.all([
    conn.getTokenAccountBalance(pool.poolBaseTokenAccount, "confirmed"),
    conn.getTokenAccountBalance(pool.poolQuoteTokenAccount, "confirmed"),
  ]);
  const B = Number(bv.value.amount), Q = Number(qv.value.amount), V = Number(pool.virtualQuoteReserves ?? 0);
  const share = Number(pos.lp) / Number(pool.lpSupply.toString());
  const myB = share * B + Number(pos.leftoverBase ?? 0), myQ = share * Q;
  const sell = Math.min(((Q + V) * myB) / (B + myB) * (1 - 0.0125), Q);
  // + lamports held in our own token / LP / WSOL accounts: rent (refunded when they close) and unwrapped leftover
  // quote; the cost basis paid them, so leaving them out made a fresh position look ~4% down instead of ~1.3%
  const wsolAta = getAssociatedTokenAddressSync(WSOL, user, false, TOKEN_PROGRAM_ID);
  const own = await conn.getMultipleAccountsInfo([new PublicKey(pos.baseAta), new PublicKey(pos.lpAta), wsolAta], "confirmed");
  const parked = own.reduce((s, a) => s + (a ? a.lamports : 0), 0);
  return (myQ + sell + parked) / LAMPORTS;
}

async function enter(mig) {
  const poolKey = mig.pool;
  const pool = await online.fetchPool(poolKey);
  if (!pool.baseMint.equals(mig.mint) || !pool.quoteMint.equals(WSOL)) return log("skip: not canonical WSOL", { pool: poolKey.toBase58() });
  if (pool.isMayhemMode) return log("skip: mayhem", { pool: poolKey.toBase58() });
  const pos = { pool: poolKey.toBase58(), mint: mig.mint.toBase58(), opened: Date.now(), sigs: [], sol: 0, phase: "buy" };
  state.open[pos.pool] = pos; save();
  try {
    const half = Math.floor((SIZE_SOL * LAMPORTS) / 2);
    const swapState = await online.swapSolanaState(poolKey, user);
    const buyIxs = await sdk.buyQuoteInput(swapState, new BN(half), SLIPPAGE_PCT);
    const s1 = await send(buyIxs, "buy");
    pos.sigs.push(s1); pos.phase = "deposit"; save();
    let liq = await online.liquiditySolanaState(poolKey, user);
    const baseBal = await waitBalance(liq.userBaseTokenAccount, new BN(1));
    if (baseBal.isZero()) throw new Error("bought tokens not visible after 10 s");
    liq = await online.liquiditySolanaState(poolKey, user);  // fresh reserves for the deposit ratio
    let { quote, lpToken } = sdk.depositAutocompleteQuoteAndLpTokenFromBase(liq, baseBal, SLIPPAGE_PCT);
    if (quote.gt(new BN(half))) ({ lpToken } = sdk.depositAutocompleteBaseAndLpTokenFromQuote(liq, new BN(half), SLIPPAGE_PCT));
    if (lpToken.isZero()) throw new Error("deposit would mint 0 LP");
    const depIxs = await sdk.depositInstructions(liq, lpToken, SLIPPAGE_PCT);
    const s2 = await send(depIxs, "deposit");
    pos.sigs.push(s2);
    const lpBal = await waitBalance(liq.userPoolTokenAccount, new BN(1));
    if (lpBal.isZero()) throw new Error("no LP tokens visible after the deposit");
    pos.lp = lpBal.toString();
    pos.leftoverBase = (await tokenBalance(liq.userBaseTokenAccount)).toString();
    pos.baseTokenProgram = liq.baseTokenProgram.toBase58();
    pos.baseAta = liq.userBaseTokenAccount.toBase58();
    pos.lpAta = liq.userPoolTokenAccount.toBase58();
    pos.phase = "hold"; pos.deposited = Date.now();
    pos.cost = -((await solDelta(s1)) + (await solDelta(s2))) / LAMPORTS;
    save();
    log("entered", { pool: pos.pool, mint: pos.mint, cost_sol: pos.cost, lp: pos.lp, leftover_base: pos.leftoverBase, sigs: pos.sigs });
  } catch (e) {
    log("entry failed: exiting whatever was acquired", { pool: pos.pool, err: String(e).slice(0, 300) });
    pos.phase = "hold"; pos.deposited = 0; pos.cost = pos.cost ?? null; save();
    await exit(pos, "entry_failed");
  }
}

async function exit(pos, reason) {
  pos.phase = "exit"; save();
  const poolKey = new PublicKey(pos.pool);
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      const liq = await online.liquiditySolanaState(poolKey, user);
      const expectLp = new BN(pos.withdrawn ? "0" : (pos.lp ?? "0"));
      const lp = expectLp.gtn(0) ? await waitBalance(liq.userPoolTokenAccount, expectLp, 8_000) : await tokenBalance(liq.userPoolTokenAccount);
      let combined = false;
      if (lp.gtn(0) && attempt === 0) {
        // One tx: withdraw + sell (leftover + the withdraw's guaranteed minimum token out) + close the LP account.
        // ~1,140 of 1,232 bytes; saves the 2-4 slots of each extra confirm round trip. Dust above the minimum is
        // swept by the sell/close steps below.
        try {
          const w = sdk.withdrawAutoCompleteBaseAndQuoteFromLpToken(liq, lp, WITHDRAW_SLIPPAGE_PCT);
          const minBase = w.base.muln(100 - WITHDRAW_SLIPPAGE_PCT).divn(100);
          const sellAmt = minBase.add(await tokenBalance(liq.userBaseTokenAccount));
          const wIxs = await sdk.withdrawInstructions(liq, lp, WITHDRAW_SLIPPAGE_PCT);
          const swapState = await online.swapSolanaState(poolKey, user);
          const sIxs = await sdk.sellBaseInput(swapState, sellAmt, EXIT_SLIPPAGE_PCT);
          const lpInfo = await conn.getAccountInfo(liq.userPoolTokenAccount, "confirmed");
          const closeLp = createCloseAccountInstruction(liq.userPoolTokenAccount, user, user, [], lpInfo.owner);
          pos.sigs.push(await send([...wIxs, ...sIxs, closeLp], "withdraw+sell")); pos.withdrawn = true; save();
          combined = true;
        } catch (e) {
          log("one-tx exit failed, falling back to separate txs", { pool: pos.pool, err: String(e).slice(0, 300) });
        }
      }
      if (!combined && lp.gtn(0) && !pos.withdrawn) {
        const ixs = await sdk.withdrawInstructions(liq, lp, EXIT_SLIPPAGE_PCT);
        pos.sigs.push(await send(ixs, "withdraw")); pos.withdrawn = true; save();
      }
      // after a separate withdraw the token balance must exceed the leftover we held before it; after the one-tx
      // exit only dust is left
      const base = pos.withdrawn && lp.gtn(0) && !combined
        ? await waitBalance(liq.userBaseTokenAccount, new BN(pos.leftoverBase ?? "0").addn(1), 8_000)
        : await tokenBalance(liq.userBaseTokenAccount);
      if (base.gtn(0)) {
        const swapState = await online.swapSolanaState(poolKey, user);
        const ixs = await sdk.sellBaseInput(swapState, base, attempt < 2 ? EXIT_SLIPPAGE_PCT : 50);
        pos.sigs.push(await send(ixs, "sell")); save();
      }
      // close our now-empty token + LP accounts (rent back) and unwrap WSOL (a native account closes with balance)
      const close = [];
      for (const ata of [liq.userBaseTokenAccount, liq.userPoolTokenAccount]) {
        const info = await conn.getAccountInfo(ata, "confirmed");
        if (info && (await tokenBalance(ata)).isZero()) close.push(createCloseAccountInstruction(ata, user, user, [], info.owner));
      }
      const wsolAta = getAssociatedTokenAddressSync(WSOL, user, false, TOKEN_PROGRAM_ID);
      if (await conn.getAccountInfo(wsolAta, "confirmed")) close.push(createCloseAccountInstruction(wsolAta, user, user, [], TOKEN_PROGRAM_ID));
      if (close.length) pos.sigs.push(await send(close, "close"));
      break;
    } catch (e) {
      log("exit attempt failed", { pool: pos.pool, attempt, err: String(e).slice(0, 300) });
      await sleep(1500);
    }
  }
  let total = 0, missing = 0;
  for (const s of pos.sigs) { const d = await solDelta(s); if (d === null) missing++; else total += d; }
  const pnl = total / LAMPORTS;
  rollDay();
  state.realizedToday += pnl;
  delete state.open[pos.pool]; save();
  const row = { t: new Date().toISOString(), pool: pos.pool, mint: pos.mint, reason, pnl_sol: pnl,
                pnl_pct: 100 * pnl / SIZE_SOL, cost_sol: pos.cost, held_s: pos.deposited ? (Date.now() - pos.deposited) / 1000 : 0,
                txs: pos.sigs.length, unpriced_txs: missing, sigs: pos.sigs };
  fs.appendFileSync(LOG_PATH, JSON.stringify(row) + "\n");
  log("closed", row);
  if (state.realizedToday <= -DAILY_LOSS_SOL) log("daily loss limit hit: no new entries until the next UTC day", { realized_today: state.realizedToday });
}

async function watch(pos) {
  while (state.open[pos.pool] && pos.phase === "hold") {
    const held = Date.now() - (pos.deposited || 0);
    if (held >= HOLD_MS) return exit(pos, "hold");
    try {
      const v = await markValue(pos);
      if (pos.cost && v < pos.cost * (1 - STOP_PCT / 100)) return exit(pos, `stop ${(100 * (v / pos.cost - 1)).toFixed(1)}%`);
    } catch (e) { log("mark failed", { pool: pos.pool, err: String(e).slice(0, 200) }); }
    await sleep(1000);
  }
}

// ------------------------------------------------------------------ main
async function canEnter() {
  rollDay();
  if (fs.existsSync(KILL_PATH)) return "KILL file";
  if (Object.keys(state.open).length >= MAX_OPEN) return "max open";
  if (state.realizedToday <= -DAILY_LOSS_SOL) return "daily loss limit";
  const bal = (await conn.getBalance(user, "confirmed")) / LAMPORTS;
  if (bal < SIZE_SOL + MIN_RESERVE_SOL + 0.01) return `balance ${bal.toFixed(4)} SOL`;
  return null;
}

async function onMigration(sig) {
  if (seen.has(sig)) return;
  seen.add(sig); state.seen = [...seen].slice(-500);
  const t0 = Date.now();
  const mig = await migrationFromSig(sig);
  if (!mig) return;
  log("migration", { sig, mint: mig.mint.toBase58(), pool: mig.pool.toBase58(), decode_ms: Date.now() - t0 });
  const why = await canEnter();
  if (why) return log("skip", { reason: why, pool: mig.pool.toBase58() });
  await sleep(Math.max(0, ENTRY_DELAY_MS - (Date.now() - t0)));
  const why2 = await canEnter();
  if (why2) return log("skip", { reason: why2, pool: mig.pool.toBase58() });
  await enter(mig);
  const pos = state.open[mig.pool.toBase58()];
  if (pos && pos.phase === "hold") watch(pos);
}

async function main() {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  const bal = (await conn.getBalance(user, "confirmed")) / LAMPORTS;
  log("start", { wallet: user.toBase58(), balance_sol: bal, size_sol: SIZE_SOL, max_open: MAX_OPEN, entry_delay_ms: ENTRY_DELAY_MS,
                 hold_ms: HOLD_MS, stop_pct: STOP_PCT, daily_loss_sol: DAILY_LOSS_SOL, open: Object.keys(state.open).length });
  for (const pos of Object.values(state.open)) {  // never leave a position behind across restarts
    log("restart: exiting open position", { pool: pos.pool, phase: pos.phase });
    exit(pos, "restart");
  }
  conn.onLogs(MIGRATION_AUTHORITY, (l) => { if (!l.err) onMigration(l.signature).catch((e) => log("migration handler error", { err: String(e).slice(0, 300) })); }, "confirmed");
  setInterval(async () => {
    rollDay();
    log("heartbeat", { open: Object.keys(state.open).length, realized_today: state.realizedToday,
                       balance_sol: (await conn.getBalance(user, "confirmed").catch(() => 0)) / LAMPORTS,
                       kill: fs.existsSync(KILL_PATH) });
  }, 60_000);
}

main().catch((e) => { log("fatal", { err: String(e) }); process.exit(1); });
