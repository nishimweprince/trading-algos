/**
 * npm run racer:backfill-costs -- [--since ISO] [--limit N] [--dry] [--all] [--phase costs|tips|both]
 *
 * Costs phase: re-measures config-cost paper tracks from their +1-slot
 * buyers' transactions (signatures re-derived from pool_buy_attempts) and
 * re-costs their horizons by delta — the same math as live setLiveCost, so
 * backfilled tracks match tracks measured live. Idempotent: measured tracks
 * are skipped unless --all.
 *
 * Tips phase: backfills per-attempt tips on pool_buy_attempts (null =
 * unfetched), the raw material for a future tip→fill curve.
 *
 * Run where the DB lives (CONFIG_PATH selects it); needs an RPC with tx
 * history. Read-only on chain; patient retries, bounded concurrency.
 */
import { loadConfig, ConfigError } from '../config/load.ts';
import { openDb } from '../persistence/db.ts';
import { Repositories } from '../persistence/repositories.ts';
import { RpcClient } from '../core/rpc.ts';
import { measureBuyCostParts, txExecutionCostParts, type RawTx } from './costs.ts';
import { recostPnlForDelta } from './paper.ts';

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

interface TrackRow {
  mint: string;
  pool_address: string | null;
  entry_slot: number | null;
  size_sol: number;
  open_price: number | null;
  open_quote_sol: number | null;
  reach_price: number;
  reach_quote_sol: number;
  horizons_json: string;
  entries_json: string | null;
  armed: number | null;
  live_cost_sol: number | null;
  live_cost_source: string | null;
  entry_model_version: number | null;
  entry_quality: string | null;
  same_slot_depletion_pct: number | null;
  entry_features_json: string | null;
  wallet_balance_sol: number | null;
  sizing_reason: string | null;
  open_quality: string | null;
  open_reject_reason: string | null;
  fill_p_json: string | null;
  landing_weights_json: string | null;
}

async function main(): Promise<void> {
  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  if (!config.rpc?.primaryHttp) throw new ConfigError('rpc.primaryHttp is required');
  const since = arg('--since') ?? new Date(Date.now() - 7 * 24 * 3_600_000).toISOString();
  const limit = Math.max(1, Number(arg('--limit') ?? 200) || 200);
  const dry = process.argv.includes('--dry');
  const all = process.argv.includes('--all');
  const phase = arg('--phase') ?? 'both';
  if (phase !== 'costs' && phase !== 'tips' && phase !== 'both') throw new ConfigError('--phase must be costs|tips|both');

  const db = openDb({ path: config.persistence.dbPath });
  const repos = new Repositories(db);
  const rpc = new RpcClient({ httpUrl: config.rpc.primaryHttp, fallbackHttpUrls: config.rpc.fallbackHttp, maxConcurrent: 4, timeoutMs: 10_000 });
  const fetchTx = (s: string): Promise<RawTx | null> => rpc.getTransactionJson(s).catch(() => null) as Promise<RawTx | null>;
  const sinceSql = since.slice(0, 19).replace('T', ' ');

  if (phase === 'costs' || phase === 'both') {
    const tracks = db
      .prepare(
        `SELECT mint, pool_address, entry_slot, size_sol, open_price, open_quote_sol, reach_price, reach_quote_sol,
                horizons_json, entries_json, armed, live_cost_sol, live_cost_source, entry_model_version, entry_quality,
                same_slot_depletion_pct, entry_features_json, wallet_balance_sol, sizing_reason, open_quality,
                open_reject_reason, fill_p_json, landing_weights_json
           FROM racer_paper_tracks WHERE created_at >= ? ${all ? '' : `AND (live_cost_source IS NULL OR live_cost_source = 'config')`} ORDER BY rowid LIMIT ?`,
      )
      .all(sinceSql, limit) as unknown as TrackRow[];
    let measured = 0;
    let failed = 0;
    for (const t of tracks) {
      const sigs = db
        .prepare(
          `SELECT a.signature AS sig FROM racer_pool_buy_attempts a JOIN racer_migrations m ON m.pool_address = a.pool_address
            WHERE m.mint = ? AND a.ok = 1 AND m.migrate_slot IS NOT NULL AND a.slot = m.migrate_slot + 1
            ORDER BY a.rowid LIMIT ?`,
        )
        .all(t.mint, config.racer.costSampleBuyers) as unknown as Array<{ sig: string }>;
      const sample = await measureBuyCostParts(fetchTx, sigs.map((s) => s.sig), config.racer.costSampleBuyers,
        { attempts: 5, delayMs: 2_000 });
      if (!sample || !(t.size_sol > 0)) {
        failed++;
        continue;
      }
      const total = sample.buyFeeSol + sample.buyTipSol + config.racer.liveSellCostSol;
      const deltaPct = (((total - (t.live_cost_sol ?? config.racer.liveCostSol)) / t.size_sol) * 100);
      const horizons = JSON.parse(t.horizons_json) as Array<{ pnl?: Record<string, number | null>; rugged?: boolean }>;
      const fill = (JSON.parse(t.fill_p_json ?? '{}') ?? {}) as Record<string, { p: number } | null>;
      for (const h of horizons) {
        if (h.rugged || !h.pnl) continue;
        // Base legs are every priced leg but open/reach (and the _adj legs the helper moves itself).
        const names = Object.keys(h.pnl).filter((n) => n !== 'open' && n !== 'reach' && !n.endsWith('_adj'));
        recostPnlForDelta(h.pnl, fill, names, deltaPct);
      }
      if (!dry) {
        repos.recordRacerPaperTrack({
          mint: t.mint, poolAddress: t.pool_address ?? '', entrySlot: t.entry_slot, sizeSol: t.size_sol,
          openPrice: t.open_price, openQuoteSol: t.open_quote_sol, reachPrice: t.reach_price, reachQuoteSol: t.reach_quote_sol,
          horizonsJson: JSON.stringify(horizons), entriesJson: t.entries_json, armed: t.armed === null ? null : t.armed === 1,
          liveCostSol: total, liveCostSource: 'measured', entryModelVersion: t.entry_model_version, entryQuality: t.entry_quality,
          sameSlotDepletionPct: t.same_slot_depletion_pct, entryFeaturesJson: t.entry_features_json,
          walletBalanceSol: t.wallet_balance_sol, sizingReason: t.sizing_reason,
          openQuality: t.open_quality, openRejectReason: t.open_reject_reason, fillPJson: t.fill_p_json,
          liveCostBuyFeeSol: sample.buyFeeSol, liveCostBuyTipSol: sample.buyTipSol,
          landingWeightsJson: t.landing_weights_json,
        });
      }
      measured++;
    }
    console.log(JSON.stringify({ phase: 'costs', dry, candidates: tracks.length, measured, failed }));
  }

  if (phase === 'tips' || phase === 'both') {
    const rows = db
      .prepare(`SELECT signature AS sig FROM racer_pool_buy_attempts WHERE tip_sol IS NULL AND created_at >= ? ORDER BY rowid LIMIT ?`)
      .all(sinceSql, limit) as unknown as Array<{ sig: string }>;
    let done = 0;
    for (const r of rows) {
      const tx = await fetchTx(r.sig);
      const parts = txExecutionCostParts(tx);
      if (parts && !dry) {
        repos.updatePoolBuyAttemptTip(r.sig, parts.tipLamports / 1e9);
        done++;
      } else if (parts) {
        done++;
      }
    }
    console.log(JSON.stringify({ phase: 'tips', dry, candidates: rows.length, measured: done }));
  }
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
