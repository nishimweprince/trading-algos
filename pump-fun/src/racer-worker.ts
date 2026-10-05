import { Keypair } from '@solana/web3.js';
import { loadConfig, ConfigError } from './config/load.ts';
import { acquireLock, LockError } from './core/lock.ts';
import { logger } from './core/logger.ts';
import { RpcClient } from './core/rpc.ts';
import { installKeepAliveDispatcher } from './core/http.ts';
import { openDb } from './persistence/db.ts';
import { Repositories } from './persistence/repositories.ts';
import { fetchBondingCurve } from './enrichment/curve.ts';
import { fetchCurveTradeTx, verifyCurveTradeTx } from './executor/curve.ts';
import { PricePoller, computePrice } from './positions/pricing.ts';
import { RacerWatcher } from './racer/watcher.ts';
import { PaperTracker } from './racer/paper.ts';
import { completionCostSol } from './racer/economics.ts';

/**
 * Racer measurement worker (pump-desk-racer; feat/migration-racer).
 *
 * Tails `launches`, admits near-complete curves with one read each, polls them
 * for would-fire / migrated / expired events, and runs first-position paper
 * tracks on migrations. Zero sends by construction: no signer, no broadcaster
 * — the buy-leg probe builds UNSIGNED bytes via trade-local and verifies shape
 * only. The migrate-bundle leg is absent until a maintained builder exists
 * (see reports/migrate-racer-verdict.md).
 *
 * Starts from the current launch tail: history is not re-processed on restart.
 */

const log = logger.child({ mod: 'racer-worker' });

async function main(): Promise<void> {
  installKeepAliveDispatcher();
  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  if (!config.racer.enabled) {
    log.info('racer disabled (racer.enabled=false) — exiting');
    return;
  }
  const lock = acquireLock(process.env.LOCK_PATH ?? `${config.persistence.dbPath}.racer.lock`);
  const db = openDb({ path: config.persistence.dbPath, busyTimeoutMs: 2_000 });
  const repos = new Repositories(db);

  const httpUrl = config.rpc?.enrichmentHttp?.trim() || config.rpc?.primaryHttp;
  if (!httpUrl) throw new ConfigError('racer worker needs rpc.enrichmentHttp (or rpc.primaryHttp)');
  const rpc = new RpcClient({
    httpUrl,
    fallbackHttpUrls: (config.rpc?.fallbackHttp ?? []).filter((u) => u !== httpUrl),
    maxConcurrent: config.racer.maxConcurrent,
    timeoutMs: config.rpc?.readTimeoutMs ?? 900,
    maxRps: config.rpc?.researchMaxRps ?? 0,
  });

  // Ephemeral payer for UNSIGNED buy-leg builds: real key shape, never funded,
  // never signs. Build timing + shape verification only.
  const ephemeralPayer = Keypair.generate().publicKey.toBase58();
  const buildBuyLeg = config.racer.buyLegProbeEnabled
    ? async (mint: string, costSol: number): Promise<{ ok: boolean; ms: number }> => {
          const start = Date.now();
          try {
            const built = await fetchCurveTradeTx({
              wallet: ephemeralPayer,
              mint,
              side: 'buy',
              amountSol: Math.max(costSol, 0.000001),
              slippagePct: config.racer.slippagePct,
              priorityFeeSol: 0,
            });
            const check = verifyCurveTradeTx(built, ephemeralPayer);
            return { ok: check.ok, ms: Date.now() - start };
          } catch (err) {
            log.warn('buy-leg probe failed', { mint, err });
            return { ok: false, ms: Date.now() - start };
          }
        }
      : undefined;

  const poller = new PricePoller(rpc, config.positions.pricePollMs, () => Date.now(), {
    commitment: config.positions.priceCommitment,
  });
  const paper = new PaperTracker(
    {
      readPrice: async (mint: string) => {
        // pump.fun mints use 6 decimals (1e9 supply × 1e6) on the Token program.
        const pool = await poller.readPoolPricing(mint, 6, false);
        if (!pool) return null;
        const price = computePrice(pool.baseReserve, pool.quoteReserveLamports, 6, pool.virtualQuoteLamports);
        if (!(price > 0)) return null;
        return { price, poolAddress: pool.poolAddress, slot: null };
      },
      sink: (t) =>
        repos.recordRacerPaper({
          mint: t.mint,
          poolAddress: t.poolAddress,
          entryPrice: t.entryPrice,
          entrySlot: t.entrySlot,
          sizeSol: t.sizeSol,
          horizonsJson: JSON.stringify(t.horizons),
        }),
    },
    { horizonsSec: config.racer.paperHorizonsSec, sizeSol: config.racer.paperSizeSol },
  );

  const watcher = new RacerWatcher(
    {
      fetchCurve: (mint) => fetchBondingCurve(rpc, mint),
      getSlot: () => rpc.getSlot('processed'),
      readPool: async (mint) => (await poller.readPoolPricing(mint, 6, false).catch(() => null)) !== null,
      sink: (e) => {
        repos.recordRacerEvent({
          mint: e.mint,
          kind: e.kind,
          slot: e.slot,
          costSol: e.costSol,
          realTokenReserves: e.realTokenReserves,
          leadSlots: e.leadSlots,
          buyLegMs: e.buyLegMs,
          buyLegOk: e.buyLegOk,
        });
        if (e.kind === 'migrated') paper.track(e.mint);
      },
      ...(buildBuyLeg ? { buildBuyLeg } : {}),
    },
    {
      maxCostSol: config.racer.maxCompletionCostSol,
      maxTracked: config.racer.maxTracked,
      trackWindowMs: config.racer.trackWindowMinutes * 60_000,
      maxConcurrent: config.racer.maxConcurrent,
    },
  );

  let launchCursor = repos.maxRowid('launches');
  let inFlight = false;
  let overlapSkips = 0;
  const tick = async () => {
    if (inFlight) {
      overlapSkips++;
      return;
    }
    inFlight = true;
    try {
      for (const l of repos.launchesAfter(launchCursor)) {
        launchCursor = l.rowid;
        // Admission: one curve read; only near-complete mints are tracked.
        const curve = await fetchBondingCurve(rpc, l.mint).catch(() => null);
        if (!curve || curve.complete) continue;
        const cost = completionCostSol(curve);
        if (cost === null || cost > config.racer.admitCostSol) continue;
        watcher.addLaunch(l.mint);
      }
      const stats = await watcher.tick();
      if (stats.events > 0) log.info('racer tick', { ...stats, tracked: watcher.size, pending: paper.pendingCount });
    } catch (err) {
      log.warn('racer tick failed', { err });
    } finally {
      inFlight = false;
    }
  };
  const timer = setInterval(() => void tick(), config.racer.pollMs);
  log.info('racer worker started', { launchCursor, pollMs: config.racer.pollMs });

  const stop = (signal: string) => {
    log.info('racer worker stopping', { signal, overlapSkips, abandoned: paper.abandoned });
    clearInterval(timer);
    db.close();
    lock.release();
    process.exit(0);
  };
  process.on('SIGINT', () => stop('SIGINT'));
  process.on('SIGTERM', () => stop('SIGTERM'));
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    if (err instanceof LockError) log.error('another racer worker holds the lock', { err: err.message });
    else log.error('racer worker failed to start', { err });
    process.exit(1);
  });
}
