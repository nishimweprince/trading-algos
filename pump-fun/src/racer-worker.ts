import { Connection, PublicKey } from '@solana/web3.js';
import { canonicalPumpPoolPda } from '@pump-fun/pump-swap-sdk';
import { loadConfig, readSecret, ConfigError, heliusApiKeyFromUrl } from './config/load.ts';
import { acquireLock, LockError } from './core/lock.ts';
import { logger } from './core/logger.ts';
import { RpcClient } from './core/rpc.ts';
import { installKeepAliveDispatcher } from './core/http.ts';
import { LAMPORTS_PER_SOL, PROGRAM_IDS } from './core/constants.ts';
import { openDb } from './persistence/db.ts';
import { Repositories } from './persistence/repositories.ts';
import { AmmConfigCache } from './executor/swapState.ts';
import { BlockhashCache } from './executor/blockhashCache.ts';
import { Wallet } from './executor/wallet.ts';
import { randomSenderTipAccount } from './executor/heliusSender.ts';
import { SendReadyProbe } from './racer/sendReady.ts';
import { PricePoller, computePrice } from './positions/pricing.ts';
import { RacerStream, type CurveUpdate } from './racer/stream.ts';
import { RacerTracker, type TrackerEvent } from './racer/tracker.ts';
import { MintResolver } from './racer/mintResolver.ts';
import { isThinPool, migrateEntry, poolOf, type MigrateTx } from './racer/migrateTx.ts';
import { WSOL_MINT } from './core/constants.ts';
import { PaperTracker, type EntryFeatureSnapshot } from './racer/paper.ts';
import type { PoolMark } from './racer/economics.ts';
import { SlotEntryTracker, decodeTokenAmount, handleTrackerVaultWatch } from './racer/slotEntry.ts';
import { median, txExecutionCostSol, type RawTx } from './racer/costs.ts';
import type { MarkRead } from './racer/paper.ts';
import { deriveAta } from './core/ata.ts';
import { PoolPathRecorder, EXIT_RULES, simulatePathExit } from './racer/pathExit.ts';

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

  // Send-ready probe: the real post-migration buy, built and signed (never
  // sent) with an ephemeral key, from state that exists before the migrate.
  const probeWallet = Wallet.load('RACER_PROBE_NO_KEY', 'dry-run');
  const ammConfigs = new AmmConfigCache(rpc, 30_000);
  const blockhashes = new BlockhashCache(new Connection(httpUrl, 'confirmed'), 20_000);
  const sendReady = new SendReadyProbe({
    configs: () => ammConfigs.get(),
    blockhash: () => blockhashes.get(),
    wallet: probeWallet,
    // Values do not change timing; a tip + priority fee keep the tx shape real.
    feePlan: { priorityMicroLamports: config.fees.priorityFloorMicroLamports, jitoTipLamports: 1_000_000, exitTipLamports: 200_000 },
    tipAccount: () => randomSenderTipAccount(),
    quoteLamports: BigInt(Math.round(rc.paperSizeSol * LAMPORTS_PER_SOL)),
    slippagePct: rc.slippagePct,
  });
  const userQuoteAta = deriveAta(probeWallet.publicKey, WSOL_MINT, false);
  const probePools = new Map<string, string>(); // pool PDA → mint (pushed pool accounts)
  /** Off the hot path: mint account + ATAs, so the migrate-time build needs no RPC. */
  const prepareSendReady = async (mint: string, poolKey: string): Promise<void> => {
    if (!rc.sendReadyProbeEnabled || sendReady.isPrepared(mint)) return;
    const mintAcct = await rpc.getAccountInfoBase64(mint, 'confirmed').catch(() => null);
    if (!mintAcct) return;
    sendReady.prepare(mint, {
      poolKey,
      baseMint: mintAcct,
      userBaseAta: deriveAta(probeWallet.publicKey, mint, mintAcct.owner === PROGRAM_IDS.TOKEN_2022),
      userQuoteAta,
    });
  };
  const SEND_KINDS = [
    ['racer_send_ready_us', 'totalUs'],
    ['racer_send_wait_pool_us', 'waitPoolUs'],
    ['racer_send_state_us', 'stateUs'],
    ['racer_send_build_us', 'buildUs'],
    ['racer_send_sign_us', 'signUs'],
  ] as const;
  const sendReadyStats = { ok: 0, failed: 0 as number, reasons: {} as Record<string, number> };
  /** Per landed migrate: receipt slot, send-ready time and the earliest modeled landing slot. */
  interface LandingTiming {
    receiptSlot: number | null;
    sendReadyUs: number | null;
    feasibleSlot: number | null;
    feasibleSlotRaw: number | null;
  }
  const landing = new Map<string, Promise<LandingTiming>>();

  // Realistic entries: base vaults armed at curve admission (see racer/slotEntry.ts).
  // Watches armed at admission must survive the full curve tracking window;
  // tracker closed/migrated events release them explicitly.
  const slotEntries = new SlotEntryTracker({ ttlMs: (rc.trackWindowMinutes * 60 + rc.completeNoPoolSec) * 1_000 });
  const pathRecorder = new PoolPathRecorder();
  const watchedPools = new Set<string>();
  const poolByMint = new Map<string, string>();
  let pushWatched: () => void = () => {};
  const releaseEntryWatches = (mint: string): void => {
    slotEntries.release(mint);
    const pool = poolByMint.get(mint);
    if (pool) watchedPools.delete(pool);
    poolByMint.delete(mint);
    pushWatched();
  };
  const releaseProbe = (mint: string): void => {
    sendReady.release(mint);
    for (const [pool, m] of probePools) if (m === mint) probePools.delete(pool);
  };
  const releaseWatches = (mint: string): void => { releaseEntryWatches(mint); pathRecorder.release(mint); releaseProbe(mint); pushWatched(); };
  const armVaults = (mint: string, early: boolean): void => {
    try {
      const pool = canonicalPumpPoolPda(new PublicKey(mint)).toBase58();
      watchedPools.add(pool);
      poolByMint.set(mint, pool);
      // Token-2022 or SPL base: watch both candidate ATAs; only one will exist.
      if (rc.sendReadyProbeEnabled) {
        probePools.set(pool, mint);
        void prepareSendReady(mint, pool);
      }
      slotEntries.arm(mint, [deriveAta(pool, mint, true), deriveAta(pool, mint, false)], early);
      pathRecorder.arm(mint, [deriveAta(pool, mint, true), deriveAta(pool, mint, false)], deriveAta(pool, WSOL_MINT, false));
      pushWatched();
    } catch (err) {
      log.debug('vault arm failed', { mint, err });
    }
  };

  const persist = (e: TrackerEvent): void => {
    handleTrackerVaultWatch(e, (mint) => armVaults(mint, true), releaseWatches);
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

  /** Pool mark, 'gone' when the canonical pool account no longer exists, null when unreadable. */
  const readMark = async (mint: string): Promise<MarkRead> => {
    const pool = await poller.readPoolPricing(mint, PUMP_DECIMALS, false);
    if (!pool) {
      try {
        const key = canonicalPumpPoolPda(new PublicKey(mint)).toBase58();
        return (await rpc.getAccountInfoBase64(key, 'confirmed')) === null ? 'gone' : null;
      } catch {
        return null;
      }
    }
    // Fully withdrawn: nothing left to price or sell into.
    if (pool.baseReserve === 0n) return 'gone';
    const price = computePrice(pool.baseReserve, pool.quoteReserveLamports, PUMP_DECIMALS, pool.virtualQuoteLamports);
    // Partly drained pools still price (the virtual quote props the mid up);
    // realQuoteSol caps what a seller can actually take out.
    if (!(price > 0) || !Number.isFinite(price)) return null;
    return {
      price,
      quoteSol: Number(pool.quoteReserveLamports + pool.virtualQuoteLamports) / LAMPORTS_PER_SOL,
      realQuoteSol: Number(pool.quoteReserveLamports) / LAMPORTS_PER_SOL,
    };
  };

  /** Median fee + tips of the buyers that landed in migrate slot +1, as a round-trip cost. */
  const measuredPathCosts = new Map<string, number>();
  const measureLiveCost = async (mint: string, signatures: string[]): Promise<void> => {
    const sample = signatures.slice(0, rc.costSampleBuyers);
    if (sample.length === 0) return;
    const costs: number[] = [];
    for (let attempt = 0; attempt < 3 && costs.length === 0; attempt++) {
      if (attempt > 0) await new Promise((r) => setTimeout(r, 1_500));
      const got = await Promise.all(sample.map((s) => rpc.getTransactionJson(s).catch(() => null)));
      for (const tx of got) {
        const c = txExecutionCostSol(tx as RawTx | null);
        if (c !== null) costs.push(c);
      }
    }
    const buy = median(costs);
    if (buy === null) return;
    measuredPathCosts.set(mint, buy + rc.liveSellCostSol);
    paper.setLiveCost(mint, buy + rc.liveSellCostSol);
    log.info('measured +1-slot buyer cost', { mint, buyers: costs.length, medianBuyCostSol: buy });
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
            entriesJson: JSON.stringify({ open: t.open, reach: t.reach, ...t.delayed }),
            armed: t.armed,
            entryModelVersion: 2,
            entryQuality: t.entryQuality,
            sameSlotDepletionPct: t.sameSlotDepletionPct,
            entryFeaturesJson: t.entryFeatures ? JSON.stringify(t.entryFeatures) : null,
            liveCostSol: t.liveCostSol,
            liveCostSource: t.liveCostSource,
          });
        } catch (err) {
          log.warn('racer paper persist failed', { mint: t.mint, err });
        }
      },
    },
    { horizonsSec: rc.paperHorizonsSec, sizeSol: rc.paperSizeSol, liveCostSol: rc.liveCostSol, exitLandMs: rc.exitLandMs },
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
    const seenAtMs = Date.now();
    // Untracked migrations still get a row, but their entry quality is unverified.
    armVaults(mint, false);
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
    if (poolKey && !watchedPools.has(poolKey)) {
      const previous = poolByMint.get(mint);
      if (previous) watchedPools.delete(previous);
      watchedPools.add(poolKey);
      poolByMint.set(mint, poolKey);
      pushWatched();
    }
    const reachBase = poolKey ? tx.post.filter((b) => b.owner === poolKey && b.mint === mint).reduce((n, b) => n + b.amount, 0n) : 0n;
    const quoteVaultLamports = poolKey ? tx.post.filter((b) => b.owner === poolKey && b.mint === WSOL_MINT).reduce((n, b) => n + b.amount, 0n) : 0n;
    const entryFeatures: EntryFeatureSnapshot = {
      version: 1,
      observedAtMs: Date.now(),
      migrateSlot: tx.slot,
      fireToCompleteSlots: st?.fireSlot != null && st.completeSlot != null ? st.completeSlot - st.fireSlot : null,
      completeToMigrateSlots: st?.completeSlot != null && tx.slot != null ? tx.slot - st.completeSlot : null,
      caller: tx.caller,
      quoteVaultSol: pool?.quoteMint === WSOL_MINT ? Number(quoteVaultLamports) / LAMPORTS_PER_SOL : null,
      tokenProgram: tx.tokenProgram ?? null,
      sameSlotDepletionPct: slotEntries.observedSameSlotDepletionPct(mint, tx.slot, tx.signature, reachBase),
      knownFailedMigrateAttempts: repos.countObservedFailedMigrateAttempts(mint, tx.slot),
    };
    let unpricedReason: string | null = null;
    let virtualQuote: bigint | null = null;
    if (!pool) unpricedReason = 'no-vaults';
    else if (pool.quoteMint !== WSOL_MINT) unpricedReason = 'non-wsol-quote';
    else {
      virtualQuote = poolKey ? await readVirtualQuote(poolKey) : null;
      if (virtualQuote === null) unpricedReason = 'no-virtual-quote';
    }
    const entry = !unpricedReason && poolKey && virtualQuote !== null ? migrateEntry(tx, mint, poolKey, virtualQuote, PUMP_DECIMALS) : null;
    if (isThinPool(entry?.reach ?? null, rc.minPaperQuoteSol)) unpricedReason = 'thin-pool';
    else if (!entry && !unpricedReason) unpricedReason = 'no-vaults';
    const timing = (await landing.get(mint)) ?? null;
    landing.delete(mint);
    if (landing.size > 2_000) landing.clear();
    repos.recordRacerMigration({
      receiptSlot: timing?.receiptSlot ?? null,
      sendReadyUs: timing?.sendReadyUs ?? null,
      feasibleSlot: timing?.feasibleSlot ?? null,
      feasibleSlotRaw: timing?.feasibleSlotRaw ?? null,
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
      initialQuoteDepthSol: entry?.open?.quoteSol ?? null,
      observedQuoteDepthSol: entry?.reach.quoteSol ?? null,
      depthThresholdSol: rc.minPaperQuoteSol,
    });
    if (entry && poolKey && !unpricedReason) {
      // Let the slots after the migrate settle, then price the realistic entries.
      const wait = rc.realisticSettleMs - (Date.now() - seenAtMs);
      if (wait > 0) await new Promise((r) => setTimeout(r, wait));
      const realistic = tx.slot !== null ? slotEntries.take(mint, tx.slot, tx.signature, entry.reach, entry.poolBase, rc.realisticSlots) : null;
      const entryAtMs = tx.slot === null ? seenAtMs + 400 : pathRecorder.midSlotEntryAt(mint, tx.slot + 1, seenAtMs + 400);
      releaseEntryWatches(mint);
      // The "feasible" leg lands at the per-migration modeled slot instead of a fixed +1.
      const k = timing?.feasibleSlot != null && tx.slot !== null ? timing.feasibleSlot - tx.slot : null;
      const feasibleLegs: Record<string, PoolMark | null> = realistic
        ? {
            feasible: k !== null && k >= 1 && k <= 3 ? (realistic.entries[`slot${k}`] ?? null) : null,
            feasiblemid: k !== null && k >= 1 && k <= 3 ? (realistic.entries[`slot${k}mid`] ?? null) : null,
          }
        : {};
      const accepted = paper.track(mint, {
        poolAddress: poolKey,
        slot: tx.slot,
        open: entry.open,
        reach: entry.reach,
        delayed: { ...(realistic?.entries ?? {}), ...feasibleLegs },
        armed: realistic ? realistic.early : null,
        entryQuality: realistic?.quality ?? 'missing-anchor',
        sameSlotDepletionPct: realistic?.sameSlotDepletionPct ?? null,
        elapsedMs: Date.now() - seenAtMs,
        entryFeatures,
      });
      if (!accepted) {
        try { repos.markRacerMigrationUnpriced(mint, 'paper-capacity'); }
        finally { pathRecorder.release(mint); pushWatched(); }
        return;
      }
      if (realistic) void measureLiveCost(mint, realistic.slot1Buyers).catch((err) => log.debug('live cost measure failed', { mint, err }));
      const maxHorizonMs = Math.max(0, ...rc.paperHorizonsSec) * 1_000;
      const pathWaitMs = Math.max(0, entryAtMs + maxHorizonMs + 10_000 - Date.now());
      setTimeout(() => {
        try {
          const captured = pathRecorder.finish(mint, tx.slot!, tx.signature, entry.poolBase, entry.poolQuoteLamports, virtualQuote!);
          pushWatched();
          const mid = realistic?.entries.slot1mid;
          const results = Object.fromEntries(EXIT_RULES.map((rule) => [rule, mid && realistic?.quality === 'verified'
            ? simulatePathExit(mid, captured, entryAtMs, rc.paperSizeSol, measuredPathCosts.get(mint) ?? rc.liveCostSol, rule, maxHorizonMs)
            : { reason: 'unpriced-exit', pnlPct: null, exitAtMs: null }]));
          repos.recordRacerPath(mint, JSON.stringify({ version: 2, entryAtMs, quality: captured.quality, gapsMs: captured.gapsMs ?? [], states: captured.states, results }), JSON.stringify(results));
          measuredPathCosts.delete(mint);
        } catch (err) { log.warn('racer path persist failed', { mint, err }); }
      }, pathWaitMs);
      return;
    }
    releaseWatches(mint);
    log.info('migration recorded without a paper track', { mint, signature: tx.signature, unpricedReason, canonical: poolKey === canonical });
  };

  // Mints whose landed migrate was handled (a replayed or forked duplicate is dropped).
  const migrated = new Set<string>();
  const stream = new RacerStream(
    {
      endpoint: grpcEndpoint,
      token: grpcToken,
      bandValues: rc.streamBandValues,
      // Writes may be missing from here on: path exits that would rely on "no trade" stay unpriced.
      onGap: (reason) => {
        pathRecorder.markGap();
        log.warn('racer stream gap — open paths marked', { reason });
      },
    },
    {
      onLaunch: (mint) => resolver.noteMint(mint),
      isWatched: (address) => slotEntries.isWatched(address) || pathRecorder.isWatched(address) || probePools.has(address),
      onWatched: (address, data, slot, signature, owner) => {
        const probeMint = probePools.get(address);
        if (probeMint) {
          if (owner === PROGRAM_IDS.PUMP_SWAP) {
            sendReady.onPool(probeMint, { data: Buffer.from(data).toString('base64'), owner, lamports: 0 });
          }
          return;
        }
        const amount = decodeTokenAmount(data);
        if (amount !== null) slotEntries.onVault(address, amount, slot, signature);
        pathRecorder.onVault(address, amount, slot, signature);
      },
      onCurve: (u) => resolver.resolve(u.curve, u.signature, u, (mint, upd) => tracker.onCurve(mint, upd.curve, upd.reserves, upd.slot)),
      onMigrateTx: (tx, receivedAtNs) => {
        // Hot path first: nothing (DB, logs, lookups) runs before the build.
        if (rc.sendReadyProbeEnabled && tx.landed && tx.mint && !migrated.has(tx.mint) && receivedAtNs !== undefined) {
          const mint = tx.mint;
          const prep = sendReady.isPrepared(mint);
          let base = 0n;
          let quote = 0n;
          for (const b of tx.post) {
            if (!b.owner || !probePools.has(b.owner)) continue;
            if (b.mint === mint) base += b.amount;
            else if (b.mint === WSOL_MINT) quote += b.amount;
          }
          const receiptSlot = stream.heartbeatSlot;
          const migrateSlot = tx.slot;
          const probed = sendReady.probe({ mint, receivedAtNs, baseReserve: base, quoteReserveLamports: quote });
          // Earliest landing slot: the slot in progress when a send leaving at
          // send-ready (+ one-way allowance) would reach the leader.
          landing.set(
            mint,
            probed
              .then((r): LandingTiming => {
                if (!r.ok || migrateSlot === null) return { receiptSlot, sendReadyUs: null, feasibleSlot: null, feasibleSlotRaw: null };
                const tNs = receivedAtNs + BigInt(Math.round(r.totalUs * 1_000 + rc.sendOneWayMs * 1_000_000));
                const raw = stream.slotInProgressAt(tNs);
                return { receiptSlot, sendReadyUs: r.totalUs, feasibleSlotRaw: raw, feasibleSlot: raw === null ? null : Math.max(migrateSlot + 1, raw) };
              })
              .catch((): LandingTiming => ({ receiptSlot, sendReadyUs: null, feasibleSlot: null, feasibleSlotRaw: null })),
          );
          void probed
            .then((r) => {
              // The migrated pool's own account is no longer needed: stop watching it.
              releaseProbe(mint);
              pushWatched();
              if (r.ok) {
                sendReadyStats.ok++;
                for (const [kind, field] of SEND_KINDS) repos.recordLatencySample({ kind, latencyMs: r[field], mint, feedSource: 'racer-probe' });
              } else {
                sendReadyStats.failed++;
                sendReadyStats.reasons[r.reason] = (sendReadyStats.reasons[r.reason] ?? 0) + 1;
                log.debug('send-ready probe not built', { mint, reason: r.reason, prepared: prep, detail: 'detail' in r ? r.detail : undefined });
              }
            })
            .catch((err) => log.warn('send-ready probe failed', { mint, err }));
        }
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
        // Detection lag in chain slots: where the heartbeat stood when the landed migrate reached us.
        const hb = stream.heartbeatSlot;
        if (hb !== null && tx.slot !== null) {
          try {
            repos.recordLatencySample({ kind: 'racer_detect_slots', latencyMs: hb - tx.slot, mint: tx.mint, feedSource: 'laserstream' });
          } catch (err) {
            log.debug('detect latency persist failed', { err });
          }
        }
        if (migrated.size > 10_000) migrated.clear();
        void onLandedMigrate(tx, tx.mint).catch((err) => log.warn('racer migration handling failed', { mint: tx.mint, err }));
      },
      isWatchedPool: (address) => watchedPools.has(address),
      onPoolBuy: (attempt) => {
        try { repos.recordRacerPoolBuyAttempt(attempt); }
        catch (err) { log.warn('racer pool-buy persist failed', { signature: attempt.signature, err }); }
      },
    },
  );
  pushWatched = () => {
    stream.setWatches([...new Set([...slotEntries.watched(), ...pathRecorder.watched(), ...probePools.keys()])], [...watchedPools]);
  };
  if (rc.sendReadyProbeEnabled) {
    ammConfigs.start();
    void blockhashes.warm();
    const bhTimer = setInterval(() => void blockhashes.warm(), 10_000);
    bhTimer.unref?.();
  }
  stream.start();

  const sweepTimer = setInterval(() => {
    tracker.sweep();
    if (slotEntries.sweep()) pushWatched();
  }, 5_000);
  const statsTimer = setInterval(() => {
    log.info('racer stats', {
      connected: stream.connected,
      sinceFrameMs: Date.now() - stream.lastFrameAtMs,
      stream: stream.stats,
      resolver: { ...resolver.stats, mapped: resolver.size },
      tracked: tracker.size,
      sendReady: { ...sendReadyStats, prepared: probePools.size },
      vaultsWatched: slotEntries.watched().length,
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
