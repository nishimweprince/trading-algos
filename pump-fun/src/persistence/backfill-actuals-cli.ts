/**
 * npm run db:backfill-actuals -- [--since 2026-10-03T00:00:00Z] [--all] [--dry]
 *
 * Re-books CLOSED live positions with wallet-true PnL read from the chain
 * (entry tx + every exit tx's pre/post balances). Idempotent: by default only
 * rows without wallet_pnl_sol are touched; --all re-books them too. Run where
 * the DB lives (CONFIG_PATH selects it); needs the wallet key and an RPC.
 */
import { loadConfig, ConfigError } from '../config/load.ts';
import { openDb } from './db.ts';
import { Repositories } from './repositories.ts';
import { RpcClient } from '../core/rpc.ts';
import { deriveAta } from '../core/ata.ts';
import { Wallet } from '../executor/wallet.ts';
import { fetchFillActuals } from '../executor/fillActuals.ts';
import { ActualsRecorder } from '../positions/actuals.ts';
import { exitSigsFrom } from '../positions/manager.ts';

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

async function main(): Promise<void> {
  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  if (!config.rpc?.primaryHttp) throw new ConfigError('rpc.primaryHttp is required');
  const since = arg('--since') ?? '2026-10-03T00:00:00Z';
  const all = process.argv.includes('--all');
  const dry = process.argv.includes('--dry');

  const db = openDb({ path: config.persistence.dbPath });
  const repos = new Repositories(db);
  const rpc = new RpcClient({ httpUrl: config.rpc.primaryHttp, fallbackHttpUrls: config.rpc.fallbackHttp, maxConcurrent: 4, timeoutMs: 5_000 });
  const wallet = Wallet.load(config.wallet.keypairEnvVar, 'live').publicKey;
  const recorder = new ActualsRecorder({
    repos,
    executor: {
      fillActuals: (sig, mint, t22 = false, opts = {}) => fetchFillActuals(rpc, sig, wallet, mint, deriveAta(wallet, mint, t22), opts),
    },
  });

  const rows = db
    .prepare(
      `SELECT rowid, mint, entry_tx AS entryTx, exit_tx AS exitTx, exit_intent_json AS intent, execution_json AS exec,
              pricing_json AS pricing, exit_reason AS reason, size_sol AS sizeSol, pnl_sol AS pnl
         FROM positions
        WHERE state = 'CLOSED' AND closed_at >= ?
          AND (mode = 'live' OR (mode IS NULL AND entry_tx IS NOT NULL)) AND COALESCE(simulated, 0) = 0
          ${all ? '' : 'AND wallet_pnl_sol IS NULL'}
        ORDER BY rowid`,
    )
    .all(since) as Array<{
    rowid: number; mint: string; entryTx: string | null; exitTx: string | null; intent: string | null; exec: string | null;
    pricing: string | null; reason: string | null; sizeSol: number; pnl: number | null;
  }>;
  console.log(`${rows.length} CLOSED live row(s) since ${since}${dry ? ' (dry run)' : ''}`);

  let booked = 0;
  let modelSum = 0;
  let walletSum = 0;
  // Buys written FAILED before 2026-10-03 lost entry_tx; the signature
  // survives in that row's execution_json (result / entry BroadcastResult).
  const lostEntrySig = (mint: string): string | null => {
    const prior = db
      .prepare(`SELECT execution_json AS j FROM positions WHERE mint = ? AND state IN ('FAILED', 'PENDING_ENTRY') ORDER BY rowid DESC`)
      .all(mint) as Array<{ j: string | null }>;
    for (const p of prior) {
      try {
        const j = p.j ? (JSON.parse(p.j) as { result?: { signature?: string; sent?: boolean }; entry?: { signature?: string } }) : {};
        const sig = j.result?.sent !== false ? j.result?.signature ?? j.entry?.signature : j.entry?.signature;
        if (sig) return sig;
      } catch {
        // skip unparsable rows
      }
    }
    return null;
  };

  for (const r of rows) {
    const t22 = parseT22(r.pricing);
    if (!r.entryTx) r.entryTx = lostEntrySig(r.mint);
    const exitTxs = [r.exitTx, ...exitSigsFrom(r.intent), ...orphanSigs(r.exec), ...intentSigsFromExec(r.exec)];
    const orphan = r.reason === 'ORPHAN_RECOVERY';
    if (dry) {
      console.log(`  ${r.mint} ${r.reason} entry=${r.entryTx ? 'y' : 'n'} exits=${new Set(exitTxs.filter(Boolean)).size} model=${(r.pnl ?? 0).toFixed(5)}`);
      continue;
    }
    const a = await recorder.bookTrade(
      { mint: r.mint, baseIsToken2022: t22, entryTx: orphan && r.sizeSol === 0 ? null : r.entryTx, exitTxs, entryOptional: orphan && r.sizeSol === 0, rowid: r.rowid },
      { attempts: 2, delayMs: 500 },
    );
    if (!a) {
      console.log(`  ${r.mint} — not booked (entry tx unreadable)`);
      continue;
    }
    booked++;
    modelSum += r.pnl ?? 0;
    walletSum += a.walletPnlSol;
    console.log(`  ${r.mint} ${r.reason}: model ${(r.pnl ?? 0).toFixed(5)} -> wallet ${a.walletPnlSol.toFixed(5)} SOL (fees ${a.feesSol.toFixed(5)})`);
  }
  if (!dry) console.log(`booked ${booked}/${rows.length}: model total ${modelSum.toFixed(5)} SOL -> wallet total ${walletSum.toFixed(5)} SOL`);
  db.close();
}

function parseT22(pricing: string | null): boolean {
  try {
    return Boolean(pricing && (JSON.parse(pricing) as { baseIsToken2022?: boolean }).baseIsToken2022);
  } catch {
    return false;
  }
}

/** Sell signatures the orphan reconciler stored in execution_json. */
function orphanSigs(exec: string | null): string[] {
  try {
    const j = exec ? (JSON.parse(exec) as { signatures?: unknown }) : {};
    return Array.isArray(j.signatures) ? j.signatures.filter((s): s is string => typeof s === 'string') : [];
  } catch {
    return [];
  }
}

/** Exit attempts recorded inside execution_json.exitIntent (live closes). */
function intentSigsFromExec(exec: string | null): string[] {
  try {
    const j = exec ? (JSON.parse(exec) as { exitIntent?: unknown }) : {};
    return j.exitIntent ? exitSigsFrom(JSON.stringify(j.exitIntent)) : [];
  } catch {
    return [];
  }
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
