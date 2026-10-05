import { Keypair, PublicKey } from '@solana/web3.js';
import { canonicalPumpPoolPda } from '@pump-fun/pump-swap-sdk';
import { loadConfig, readSecret, ConfigError, heliusApiKeyFromUrl } from './config/load.ts';
import { acquireLock, LockError } from './core/lock.ts';
import { logger } from './core/logger.ts';
import { RpcClient } from './core/rpc.ts';
import { installKeepAliveDispatcher } from './core/http.ts';
import { LAMPORTS_PER_SOL } from './core/constants.ts';
import { openDb } from './persistence/db.ts';
import { Repositories } from './persistence/repositories.ts';
import { fetchCurveTradeTx, verifyCurveTradeTx } from './executor/curve.ts';
import { PricePoller, computePrice } from './positions/pricing.ts';
import { RacerStream, type CurveUpdate } from './racer/stream.ts';
import { RacerTracker, type TrackerEvent } from './racer/tracker.ts';
import { MintResolver } from './racer/mintResolver.ts';
import { migrateEntry, poolOf, type MigrateTx } from './racer/migrateTx.ts';
import { WSOL_MINT } from './core/constants.ts';
import { PaperTracker } from './racer/paper.ts';
import type { PoolMark } from './racer/economics.ts';

/**
 * Racer measurement worker (pump-desk-racer; feat/migration-racer).
 *
 * One LaserStream subscription pushes near-complete bonding curves, every
 * migrate attempt (landed and failed) and pump.fun creations (see
 * racer/stream.ts). The tracker turns those into chain-slot race timings;
 * each landed migration is priced at the pool's open and first-reachable
 * marks straight from the migrate tx and paper-tracked at fixed horizons.
 *
 * Zero sends by construction: no signer, no broadcaster — the buy-leg probe
 * builds UNSIGNED bytes via trade-local and verifies shape only. The
 * migrate-bundle leg is absent until a maintained builder exists (see
 * reports/migrate-racer-verdict.md).
 */

const log = logger.child({ mod: 'racer-worker' });
/** pump.fun mints use 6 decimals. */
const PUMP_DECIMALS = 6;

async function main(): Promise<void> {
  installKeepAliveDispatcher();
  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  const rc = config.racer;
  if (!rc.enabled) {
    log.info('racer disabled (racer.enabled=false) — exiting');
    return;
  }
  const grpcEndpoint = config.rpc?.primaryGrpc?.trim();
  if (!grpcEndpoint) throw new ConfigError('racer worker needs rpc.primaryGrpc (LaserStream)');
  const grpcToken =
    (config.rpc?.primaryGrpcTokenEnvVar ? readSecret(config.rpc.primaryGrpcTokenEnvVar) : undefined) ??
    heliusApiKeyFromUrl(config.rpc?.primaryHttp) ??
    '';
  const httpUrl = config.rpc?.enrichmentHttp?.trim() || config.rpc?.primaryHttp;
  if (!httpUrl) throw new ConfigError('racer worker needs rpc.enrichmentHttp (or rpc.primaryHttp)');

  const lock = acquireLock(process.env.LOCK_PATH ?? `${config.persistence.dbPath}.racer.lock`);
  const db = openDb({ path: config.persistence.dbPath, busyTimeoutMs: 2_000 });
  const repos = new Repositories(db);
  const rpc = new RpcClient({
    httpUrl,
    fallbackHttpUrls: (config.rpc?.fallbackHttp ?? []).filter((u) => u !== httpUrl),
    maxConcurrent: rc.maxConcurrent,
    timeoutMs: config.rpc?.readTimeoutMs ?? 900,
    maxRps: config.rpc?.researchMaxRps ?? 0,
  });
  const poller = new PricePoller(rpc, config.positions.pricePollMs, () => Date.now(), { commitment: 'processed' });

  // Ephemeral payer for UNSIGNED buy-leg builds: real key shape, never funded,
  // never signs. Build timing + shape verification only.
  const ephemeralPayer = Keypair.generate().publicKey.toBase58();
  const probeBuyLeg = async (mint: string, costSol: number): Promise<{ ok: boolean; ms: number }> => {
    const start = Date.now();
    try {
      const built = await fetchCurveTradeTx({
        wallet: ephemeralPayer,
        mint,
        side: 'buy',
        amountSol: Math.max(costSol, 0.000001),
        slippagePct: rc.slippagePct,
        priorityFeeSol: 0,
      });
      return { ok: verifyCurveTradeTx(built, ephemeralPayer).ok, ms: Date.now() - start };
    } catch (err) {
      log.debug('buy-leg probe failed', { mint, err });
      return { ok: false, ms: Date.now() - start };
    }
  };

  const persist = (e: TrackerEvent): void => {
    try {
      if (e.kind === 'migrated') {
        if (e.state) repos.upsertRacerCurve({ mint: e.mint, curve: e.state.curve, outcome: 'migrated' });
        return;
      }
      const s = e.state;
      repos.upsertRacerCurve({
        mint: s.mint,
        curve: s.curve,
        admitSlot: s.admitSlot,
        admitCostSol: s.admitCostSol,
        fireSlot: s.fireSlot,
        fireCostSol: s.fireCostSol,
        completeSlot: s.completeSlot,
        outcome: e.kind === 'closed' ? e.outcome : null,
      });
      if (e.kind === 'would-fire' && rc.buyLegProbeEnabled && s.fireCostSol !== null) {
        void probeBuyLeg(s.mint, s.fireCostSol).then((leg) =>
          repos.upsertRacerCurve({ mint: s.mint, curve: s.curve, buyLegMs: leg.ms, buyLegOk: leg.ok }),
        );
      }
    } catch (err) {
      log.warn('racer persist failed', { kind: e.kind, err });
    }
  };

  const tracker = new RacerTracker(
    {
      admitCostSol: rc.admitCostSol,
      maxCostSol: rc.maxCompletionCostSol,
      feeBps: rc.curveFeeBps,
      trackWindowMs: rc.trackWindowMinutes * 60_000,
      completeNoPoolMs: rc.completeNoPoolSec * 1000,
      maxTracked: rc.maxTracked,
    },
    persist,
  );

  const resolver = new MintResolver<CurveUpdate>((sig) => rpc.getTransactionAccountKeys(sig), {
    maxInFlight: rc.maxConcurrent * 2,
  });
  // Oldest first so the newest launches survive the map's size cap.
  for (const mint of repos.recentLaunchMints(rc.bootLaunches).reverse()) resolver.noteMint(mint);

  const readMark = async (mint: string): Promise<PoolMark | null> => {
    const pool = await poller.readPoolPricing(mint, PUMP_DECIMALS, false);
    if (!pool) return null;
    const price = computePrice(pool.baseReserve, pool.quoteReserveLamports, PUMP_DECIMALS, pool.virtualQuoteLamports);
    if (!(price > 0)) return null;
    return { price, quoteSol: Number(pool.quoteReserveLamports + pool.virtualQuoteLamports) / LAMPORTS_PER_SOL };
  };

  const paper = new PaperTracker(
    {
      readMark,
      sink: (t) => {
        try {
          repos.recordRacerPaperTrack({
            mint: t.mint,
            poolAddress: t.poolAddress,
            entrySlot: t.entrySlot,
            sizeSol: t.sizeSol,
            openPrice: t.open?.price ?? null,
            openQuoteSol: t.open?.quoteSol ?? null,
            reachPrice: t.reach.price,
            reachQuoteSol: t.reach.quoteSol,
            horizonsJson: JSON.stringify(t.horizons),
          });
        } catch (err) {
          log.warn('racer paper persist failed', { mint: t.mint, err });
        }
      },
    },
    { horizonsSec: rc.paperHorizonsSec, sizeSol: rc.paperSizeSol },
  );

  const readVirtualQuote = async (poolKey: string): Promise<bigint | null> => {
    for (let i = 0; i < 4; i++) {
      const v = await poller.readVirtualQuote(poolKey);
      if (v !== null) return v;
      await new Promise((r) => setTimeout(r, 400));
    }
    return null;
  };

  const onLandedMigrate = async (tx: MigrateTx, mint: string): Promise<void> => {
    const st = tracker.get(mint) ?? null;
    tracker.onMigrate(mint, tx.slot);
    let canonical: string | null = null;
    try {
      canonical = canonicalPumpPoolPda(new PublicKey(mint)).toBase58();
    } catch {
      canonical = null;
    }
    // The pool is whoever holds the base vault; canonical pools match the PDA.
    const pool = poolOf(tx, mint);
    const poolKey = pool?.poolOwner ?? canonical;
    let unpricedReason: string | null = null;
    let virtualQuote: bigint | null = null;
    if (!pool) unpricedReason = 'no-vaults';
    else if (pool.quoteMint !== WSOL_MINT) unpricedReason = 'non-wsol-quote';
    else {
      virtualQuote = poolKey ? await readVirtualQuote(poolKey) : null;
      if (virtualQuote === null) unpricedReason = 'no-virtual-quote';
    }
    const entry = !unpricedReason && poolKey && virtualQuote !== null ? migrateEntry(tx, mint, poolKey, virtualQuote, PUMP_DECIMALS) : null;
    if (entry && entry.reach.quoteSol < rc.minPaperQuoteSol) unpricedReason = 'thin-pool';
    else if (!entry && !unpricedReason) unpricedReason = 'no-vaults';
    repos.recordRacerMigration({
      mint,
      signature: tx.signature,
      migrateSlot: tx.slot,
      caller: tx.caller,
      tracked: st !== null,
      admitSlot: st?.admitSlot ?? null,
      fireSlot: st?.fireSlot ?? null,
      completeSlot: st?.completeSlot ?? null,
      curveBuyBefore: tx.curveBuyBefore,
      poolBuyAfter: tx.poolBuyAfter,
      bundledTokens: entry ? entry.bundledTokens.toString() : null,
      poolAddress: poolKey,
      openPrice: entry?.open?.price ?? null,
      reachPrice: entry?.reach.price ?? null,
      reachQuoteSol: entry?.reach.quoteSol ?? null,
      poolOwner: pool?.poolOwner ?? null,
      quoteMint: pool?.quoteMint ?? null,
      unpricedReason,
    });
    if (entry && poolKey && !unpricedReason) paper.track(mint, { poolAddress: poolKey, slot: tx.slot, open: entry.open, reach: entry.reach });
    else log.info('migration recorded without a paper track', { mint, signature: tx.signature, unpricedReason, canonical: poolKey === canonical });
  };

  // Mints whose landed migrate was handled (a replayed or forked duplicate is dropped).
  const migrated = new Set<string>();
  const stream = new RacerStream(
    { endpoint: grpcEndpoint, token: grpcToken, bandValues: rc.streamBandValues },
    {
      onLaunch: (mint) => resolver.noteMint(mint),
      onCurve: (u) => resolver.resolve(u.curve, u.signature, u, (mint, upd) => tracker.onCurve(mint, upd.curve, upd.reserves, upd.slot)),
      onMigrateTx: (tx) => {
        try {
          repos.recordRacerMigrateTx({
            signature: tx.signature,
            mint: tx.mint,
            slot: tx.slot,
            ok: tx.ok,
            landed: tx.landed,
            caller: tx.caller,
            curveBuyBefore: tx.curveBuyBefore,
            poolBuyAfter: tx.poolBuyAfter,
          });
        } catch (err) {
          log.warn('racer migrate-tx persist failed', { err });
        }
        if (!tx.mint) return;
        resolver.noteMint(tx.mint);
        if (!tx.landed || migrated.has(tx.mint)) return;
        migrated.add(tx.mint);
        if (migrated.size > 10_000) migrated.clear();
        void onLandedMigrate(tx, tx.mint).catch((err) => log.warn('racer migration handling failed', { mint: tx.mint, err }));
      },
    },
  );
  stream.start();

  const sweepTimer = setInterval(() => tracker.sweep(), 5_000);
  const statsTimer = setInterval(() => {
    log.info('racer stats', {
      connected: stream.connected,
      sinceFrameMs: Date.now() - stream.lastFrameAtMs,
      stream: stream.stats,
      resolver: { ...resolver.stats, mapped: resolver.size },
      tracked: tracker.size,
      paperPending: paper.pendingCount,
      paperAbandoned: paper.abandoned,
    });
  }, 60_000);
  log.info('racer worker started', { bandValues: rc.streamBandValues, admitCostSol: rc.admitCostSol, maxCostSol: rc.maxCompletionCostSol, mapped: resolver.size });

  const stop = (signal: string) => {
    log.info('racer worker stopping', { signal, stream: stream.stats, abandoned: paper.abandoned });
    clearInterval(sweepTimer);
    clearInterval(statsTimer);
    stream.stop();
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
