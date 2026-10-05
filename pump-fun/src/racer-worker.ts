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
import { BOOT_SEED_POOL, SendReadyProbe, pumpMintAccount, PUMP_MINT_SUPPLY } from './racer/sendReady.ts';
import { decodeMint, decodeTransferFee } from './enrichment/mint.ts';
import { PricePoller, computePrice } from './positions/pricing.ts';
import { RacerStream, type CurveUpdate } from './racer/stream.ts';
import { RacerTracker, type TrackerEvent } from './racer/tracker.ts';
import { MintResolver } from './racer/mintResolver.ts';
import { isThinPool, migrateEntry, poolOf, type MigrateTx } from './racer/migrateTx.ts';
import { checkOpenMark, checkOpenSlotDrift } from './racer/markChecks.ts';
import { attemptCostSol, legFill, rateFor, type LegFill } from './racer/fills.ts';
import { blendFillForLanding, tallyLandingOffsets, type LandingWeights } from './racer/landing.ts';
import { screenRacerTrack } from './racer/screening.ts';
import { HotScreen, type HotScreenResult } from './racer/hotScreen.ts';
import { WSOL_MINT } from './core/constants.ts';
import { PaperTracker, type EntryFeatureSnapshot } from './racer/paper.ts';
import { paperSizeFromBalance } from './racer/walletSize.ts';
import type { PoolMark } from './racer/economics.ts';
import { SlotEntryTracker, decodeTokenAmount, handleTrackerVaultWatch } from './racer/slotEntry.ts';
import { measureBuyCostParts, type RawTx } from './racer/costs.ts';
import type { MarkRead } from './racer/paper.ts';
import { deriveAta } from './core/ata.ts';
import { PoolPathRecorder, EXIT_RULES, simulateFsmExit, simulatePathExit, horizonExitFromPath } from './racer/pathExit.ts';
import { exitCfgFor } from './exits/engine.ts';

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

  // Read-only wallet identity for paper sizing (balance reads only — the racer
  // still has no broadcaster and the probe signs with its own ephemeral key).
  // An explicit pubkey wins; otherwise the pubkey is derived from the trading
  // wallet secret when the shared .env carries one. Only the pubkey string is
  // kept; nothing here can sign with or send from the wallet.
  let walletPubkey: string | null = null;
  {
    const explicit = (process.env[rc.walletPubkeyEnvVar] ?? '').trim();
    if (explicit) {
      try {
        walletPubkey = new PublicKey(explicit).toBase58();
      } catch {
        log.warn('invalid wallet pubkey — falling back to the trading wallet pubkey', { envVar: rc.walletPubkeyEnvVar });
      }
    }
    if (!walletPubkey && readSecret(config.wallet.keypairEnvVar)) {
      try {
        const w = Wallet.load(config.wallet.keypairEnvVar, 'dry-run');
        if (!w.ephemeral) walletPubkey = w.publicKey;
      } catch (err) {
        log.warn('trading wallet pubkey unreadable — paper falls back to the fixed size', { err });
      }
    }
  }
  log.info('racer wallet (read-only, sizing only — never sent from)', { pubkey: walletPubkey ?? 'none' });
  let cachedBalanceSol: number | null = null;
  let cachedBalanceAtMs = 0;
  const readWalletBalanceSol = async (): Promise<number | null> => {
    if (!walletPubkey) return null;
    if (cachedBalanceSol !== null && Date.now() - cachedBalanceAtMs < rc.walletBalanceCacheMs) return cachedBalanceSol;
    try {
      cachedBalanceSol = Number(await rpc.getBalance(walletPubkey, 'confirmed')) / LAMPORTS_PER_SOL;
      cachedBalanceAtMs = Date.now();
    } catch (err) {
      log.debug('racer wallet balance read failed — keeping the last value', { err });
    }
    return cachedBalanceSol;
  };
  void readWalletBalanceSol();

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
    // Fixed at the max: build/sign timing and the tx shape do not depend on the amount.
    quoteLamports: BigInt(Math.round(rc.paperSizeSol * LAMPORTS_PER_SOL)),
    slippagePct: rc.slippagePct,
  });
  const userQuoteAta = deriveAta(probeWallet.publicKey, WSOL_MINT, false);
  const probePools = new Map<string, string>(); // pool PDA → mint (pushed pool accounts)
  /** Off the hot path: mint account + ATAs, so the migrate-time build needs no RPC. */
  /** Coin creator per mint, from the curve pushes (the pool's coinCreator). */
  const creators = new Map<string, string>();
  const prepareSendReady = async (mint: string, poolKey: string): Promise<void> => {
    if (!rc.sendReadyProbeEnabled || sendReady.isPrepared(mint)) return;
    let mintAcct: Awaited<ReturnType<typeof rpc.getAccountInfoBase64>> = null;
    for (let attempt = 0; attempt < 3 && !mintAcct; attempt++) {
      if (attempt > 0) await new Promise((r) => setTimeout(r, 250));
      if (sendReady.isPrepared(mint)) return; // the migrate-time fallback got there first
      mintAcct = await rpc.getAccountInfoBase64(mint, 'confirmed').catch(() => null);
    }
    if (!mintAcct || sendReady.isPrepared(mint)) return;
    // Hot-path screen inputs, precomputed now (off the send path): the T22
    // transfer fee from this same mint read, the creator, the DB answers.
    const isToken2022 = mintAcct.owner === PROGRAM_IDS.TOKEN_2022;
    let transferFee: { bps: number; maxRaw: string } | null | 'unknown' = null;
    if (isToken2022) {
      try {
        const fee = decodeTransferFee(mintAcct.data, mintAcct.owner);
        transferFee = fee ? { bps: fee.bps, maxRaw: fee.maxRaw.toString() } : null;
      } catch {
        transferFee = 'unknown';
      }
      prepTransferFees.set(mint, transferFee);
    }
    try {
      hotScreen.prepare(mint, { creator: creators.get(mint) ?? null, isToken2022, transferFee });
    } catch (err) {
      log.debug('hot screen prep failed', { mint, err });
    }
    await sendReady.prepare(
      mint,
      {
        poolKey,
        baseMint: mintAcct,
        userBaseAta: deriveAta(probeWallet.publicKey, mint, mintAcct.owner === PROGRAM_IDS.TOKEN_2022),
        userQuoteAta,
      },
      creators.get(mint) ?? null,
    );
  };
  const synthStats = { match: 0, mismatch: 0, unverified: 0 };
  /** Audit a synthesized build against the real pool, then stop watching it. */
  const verifySynth = (mint: string): void => {
    void sendReady
      .verify(mint)
      .then((check) => {
        if (!check) return;
        if (check.match) synthStats.match++;
        else synthStats.mismatch++;
        repos.recordLatencySample({ kind: 'racer_pool_synth_match', latencyMs: check.match ? 1 : 0, mint, feedSource: 'racer-probe' });
        repos.recordLatencySample({ kind: 'racer_pool_synth_vdelta', latencyMs: Number(check.vDeltaLamports), mint, feedSource: 'racer-probe' });
        if (!check.match) log.warn('synthesized pool built different accounts than the real pool', { mint });
        releaseProbe(mint);
        pushWatched();
      })
      .catch((err) => log.warn('synthesized pool audit failed', { mint, err }));
  };
  // Decision-time guardrail screen (racer/hotScreen.ts): one engine, inputs precomputed at admission.
  const hotScreen = new HotScreen(config, repos);
  const hotVerdicts = new Map<string, HotScreenResult>();
  const prepTransferFees = new Map<string, { bps: number; maxRaw: string } | null | 'unknown'>();
  const SEND_KINDS = [
    ['racer_send_ready_us', 'totalUs'],
    ['racer_send_state_us', 'stateUs'],
    ['racer_send_build_us', 'buildUs'],
    ['racer_send_sign_us', 'signUs'],
  ] as const;
  const sendReadyStats = { ok: 0, failed: 0 as number, reasons: {} as Record<string, number> };
  /** Per landed migrate: receipt slot, send-ready time, the point landing slot, and the trailing landing distribution. */
  interface LandingTiming {
    receiptSlot: number | null;
    sendReadyUs: number | null;
    feasibleSlot: number | null;
    feasibleSlotRaw: number | null;
    landingWeights: LandingWeights | null;
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
    hotScreen.release(mint);
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
    if (e.kind === 'complete') hotScreen.refresh(e.state.mint);
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
  /** Per-track paper size (wallet-sized), so path exits cost the same size the horizons did. */
  const paperSizes = new Map<string, number>();
  const measureLiveCost = async (mint: string, signatures: string[]): Promise<void> => {
    const sample = await measureBuyCostParts(
      (s) => rpc.getTransactionJson(s).catch(() => null) as Promise<RawTx | null>,
      signatures, rc.costSampleBuyers);
    if (!sample) return;
    const total = sample.buyFeeSol + sample.buyTipSol + rc.liveSellCostSol;
    measuredPathCosts.set(mint, total);
    paper.setLiveCost(mint, total, { buyFeeSol: sample.buyFeeSol, buyTipSol: sample.buyTipSol });
    log.info('measured +1-slot buyer cost', { mint, buyers: sample.n, medianBuyFeeSol: sample.buyFeeSol, medianBuyTipSol: sample.buyTipSol });
  };

  /** Per tracked migration: what a stream-priced horizon exit needs. */
  const pathCtx = new Map<string, {
    migrateSlot: number;
    signature: string;
    reachBase: bigint;
    reachQuote: bigint;
    virtualQuote: bigint;
    anchor: PoolMark;
    entryAtMs: number;
  }>();
  const pathExitStats = { path: 0, fallback: 0 };
  const readPathExit = async (mint: string): Promise<{ mid: MarkRead; exit: MarkRead; fill: string } | null> => {
    const ctx = pathCtx.get(mint);
    if (!ctx) {
      pathExitStats.fallback++;
      return null;
    }
    const horizonAtMs = Date.now();
    // Let the next slot's states arrive: the sell lands in a later slot.
    await new Promise((r) => setTimeout(r, rc.pathExitWindowMs));
    const snap = pathRecorder.snapshot(mint, ctx.migrateSlot, ctx.signature, ctx.reachBase, ctx.reachQuote, ctx.virtualQuote);
    const h = horizonExitFromPath(snap, { mark: ctx.anchor, slot: ctx.migrateSlot }, ctx.entryAtMs, horizonAtMs);
    if (!h) {
      pathExitStats.fallback++;
      return null;
    }
    pathExitStats.path++;
    return { mid: h.mid, exit: h.exit, fill: h.fill };
  };

  const paper = new PaperTracker(
    {
      readPathExit,
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
            walletBalanceSol: t.walletBalanceSol,
            sizingReason: t.sizingReason,
            openQuality: t.openQuality,
            openRejectReason: t.openRejectReason,
            fillPJson: JSON.stringify(t.fill),
            liveCostBuyFeeSol: t.liveCostBuyFeeSol,
            liveCostBuyTipSol: t.liveCostBuyTipSol,
            landingWeightsJson: t.landingWeights ? JSON.stringify(t.landingWeights) : null,
            screeningJson: t.screening ? JSON.stringify(t.screening) : null,
          });
        } catch (err) {
          log.warn('racer paper persist failed', { mint: t.mint, err });
        }
      },
    },
    { horizonsSec: rc.paperHorizonsSec, sizeSol: rc.paperSizeSol, liveCostSol: rc.liveCostSol, exitLandMs: rc.exitLandMs,
      maxEntrySlippagePct: Math.max(config.entry.maxSlippagePct, ...config.entry.buyRetrySlippageTiers),
      maxExitSlippagePct: config.exits.ladderSlippageTiers[config.exits.ladderSlippageTiers.length - 1] },
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
    entryFeatures.bundledTokens = entry ? entry.bundledTokens.toString() : null;
    entryFeatures.poolCanonical = canonical === null || !poolKey ? null : poolKey === canonical;
    if (tx.tokenProgram === PROGRAM_IDS.TOKEN_2022) {
      // T22 transfer fee: live P0 vetoes fee-carrying mints and _adv prices
      // the fee — read the mint once at entry (best-effort; 'unknown' on failure).
      // Read at admission when the mint was prepared; RPC only otherwise.
      const prepFee = prepTransferFees.get(mint);
      prepTransferFees.delete(mint);
      if (prepFee !== undefined) entryFeatures.transferFee = prepFee;
      else
        try {
          const acct = await rpc.getAccountInfoBase64(mint, 'confirmed').catch(() => null);
          const fee = acct ? decodeTransferFee(acct.data, acct.owner) : null;
          entryFeatures.transferFee = fee ? { bps: fee.bps, maxRaw: fee.maxRaw.toString() } : (acct ? null : 'unknown');
        } catch {
          entryFeatures.transferFee = 'unknown';
        }
    }
    else if (!entry && !unpricedReason) unpricedReason = 'no-vaults';
    // Stage-1 mark check (needs only the migrate tx): a misattributed bundled
    // buy collapses the backed-out open ~1000x — reject it, never store it.
    const openCheck = entry
      ? checkOpenMark(entry.open, entry.reach, entry.bundledTokens, entry.poolBase, { maxDrainShare: rc.maxOpenDrainShare })
      : { open: null, reason: null };
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
      openPrice: openCheck.open?.price ?? null,
      openRejectReason: openCheck.reason,
      coinCreator: creators.get(mint) ?? null,
      reachPrice: entry?.reach.price ?? null,
      reachQuoteSol: entry?.reach.quoteSol ?? null,
      poolOwner: pool?.poolOwner ?? null,
      quoteMint: pool?.quoteMint ?? null,
      unpricedReason,
      initialQuoteDepthSol: openCheck.open?.quoteSol ?? null,
      observedQuoteDepthSol: entry?.reach.quoteSol ?? null,
      depthThresholdSol: rc.minPaperQuoteSol,
    });
    if (entry && poolKey && !unpricedReason) {
      // Let the slots after the migrate settle, then price the realistic entries.
      const wait = rc.realisticSettleMs - (Date.now() - seenAtMs);
      if (wait > 0) await new Promise((r) => setTimeout(r, wait));
      const realistic = tx.slot !== null ? slotEntries.take(mint, tx.slot, tx.signature, entry.reach, entry.poolBase, rc.realisticSlots) : null;
      // Stage-2 mark check: the entry-time open must agree with the first slot
      // mark. A late rejection NULLs the already-stored migration open too.
      // Only the open is nulled: it is the backed-out (fragile) leg, while the
      // reach is a direct vault read and the slot legs are corroborated again
      // by the horizon exit marks. A firing drift check means poolKey-level
      // corruption — investigate, the reason column keeps the trail.
      const firstSlotMid = realistic?.entries.slot1mid?.price ?? realistic?.entries.slot1?.price ?? null;
      const driftReason = checkOpenSlotDrift(openCheck.open, firstSlotMid, { maxSlotDrift: rc.maxOpenSlotDrift });
      const finalOpen = driftReason ? null : openCheck.open;
      const finalOpenReason = driftReason ?? openCheck.reason;
      if (driftReason && openCheck.open) {
        try { repos.markRacerMigrationOpenRejected(mint, driftReason); } catch (err) { log.debug('open rejection persist failed', { mint, err }); }
      }
      const entryAtMs = tx.slot === null ? seenAtMs + 400 : pathRecorder.midSlotEntryAt(mint, tx.slot + 1, seenAtMs + 400);
      releaseEntryWatches(mint);
      // The "feasible" leg lands at the per-migration modeled slot instead of a fixed +1 —
      // distributed over the trailing landing offsets when the full 1..3 window is priced.
      const k = timing?.feasibleSlot != null && tx.slot !== null ? timing.feasibleSlot - tx.slot : null;
      const landingWeights = timing?.landingWeights ?? null;
      const distributed = landingWeights !== null && realistic !== null && [1, 2, 3].every((s) => rc.realisticSlots.includes(s));
      const feasibleLegs: Record<string, PoolMark | null> = realistic
        ? distributed
          ? { feasible: null, feasiblemid: null }
          : {
              feasible: k !== null && k >= 1 && k <= 3 ? (realistic.entries[`slot${k}`] ?? null) : null,
              feasiblemid: k !== null && k >= 1 && k <= 3 ? (realistic.entries[`slot${k}mid`] ?? null) : null,
            }
        : {};
      if (tx.slot !== null && virtualQuote !== null) {
        pathCtx.set(mint, {
          migrateSlot: tx.slot,
          signature: tx.signature,
          reachBase: entry.poolBase,
          reachQuote: entry.poolQuoteLamports,
          virtualQuote,
          anchor: entry.reach,
          entryAtMs,
        });
      }
      // Fill model for the expectancy legs: trailing land probability per slot
      // offset, conditioned on this migration's contention. Missing buckets =
      // no `_adj` legs (fail open, base legs unchanged).
      const fill: Record<string, LegFill | null> = {};
      try {
        const stats = repos.trailingFillStats(rc.fillLookbackHours);
        const contested = (entryFeatures?.knownFailedMigrateAttempts ?? 0) > 0;
        // Expected exit-send failures per leg (same ordinary-send prior as the FSM).
        const exitP = rateFor(stats, 1, false);
        const exitFailCostSol = exitP === null ? 0 : (attemptCostSol(exitP, stats.meanFailedFeeSol) ?? 0);
        const withExit = (lf: LegFill | null): LegFill | null => (lf ? { ...lf, exitFailCostSol } : null);
        for (const slot of rc.realisticSlots) {
          const lf = withExit(legFill(stats, slot, contested));
          fill[`slot${slot}`] = lf;
          fill[`slot${slot}mid`] = lf;
        }
        if (distributed && landingWeights) {
          // Blend over the window (components already carry exitFailCostSol).
          for (const suffix of ['', 'mid'] as const) {
            fill[suffix === '' ? 'feasible' : 'feasiblemid'] = blendFillForLanding(
              {
                slot1: fill[`slot1${suffix}`] ?? null,
                slot2: fill[`slot2${suffix}`] ?? null,
                slot3: fill[`slot3${suffix}`] ?? null,
              },
              landingWeights,
            );
          }
        } else if (k !== null && k >= 1 && k <= 3) {
          const kf = withExit(legFill(stats, k, contested));
          fill.feasible = kf;
          fill.feasiblemid = kf;
        }
      } catch (err) {
        log.debug('fill model unavailable — base legs only', { mint, err });
      }
      // Size this track from the real wallet balance (cached; off the hot path).
      // Fees and landing stay simulated; the sizing reason flags every fallback.
      const sizing = paperSizeFromBalance(await readWalletBalanceSol(), {
        floorSol: config.wallet.balanceFloorSol,
        headroomSol: config.wallet.gasHeadroomSol,
        minSol: rc.minPaperSizeSol,
        maxSol: rc.paperSizeSol,
        walletConfigured: walletPubkey !== null,
      });
      // Screening parity: run the live guardrail engine over the synthesized
      // candidate so paper knows what the bot would have taken. Never blocks.
      let screening: Parameters<typeof paper.track>[1]['screening'] = null;
      const hot = hotVerdicts.get(mint);
      hotVerdicts.delete(mint);
      // The decision-time verdict from the send path; post-hoc only when the probe did not screen.
      if (hot?.verdict) screening = hot.verdict;
      else try {
        screening = screenRacerTrack(
          {
            mint, poolAddress: poolKey, migrateSlot: tx.slot, reachQuoteSol: entry.reach.quoteSol,
            quoteVaultSol: entryFeatures.quoteVaultSol, coinCreator: creators.get(mint) ?? '',
            isToken2022: tx.tokenProgram === PROGRAM_IDS.TOKEN_2022, transferFee: entryFeatures.transferFee,
          },
          config, repos);
      } catch (err) {
        log.debug('screening unavailable — track unscreened', { mint, err });
      }
      const accepted = paper.track(mint, {
        poolAddress: poolKey,
        slot: tx.slot,
        sizeSol: sizing.sizeSol,
        walletBalanceSol: sizing.walletBalanceSol,
        sizingReason: sizing.reason,
        open: finalOpen,
        openQuality: finalOpenReason ? 'rejected' : finalOpen ? 'ok' : null,
        openRejectReason: finalOpenReason,
        reach: entry.reach,
        delayed: { ...(realistic?.entries ?? {}), ...feasibleLegs },
        fill,
        landingWeights: distributed ? landingWeights : null,
        screening,
        armed: realistic ? realistic.early : null,
        entryQuality: realistic?.quality ?? 'missing-anchor',
        sameSlotDepletionPct: realistic?.sameSlotDepletionPct ?? null,
        elapsedMs: Date.now() - seenAtMs,
        entryFeatures,
      });
      if (!accepted) {
        try { repos.markRacerMigrationUnpriced(mint, 'paper-capacity'); }
        finally { pathRecorder.release(mint); pathCtx.delete(mint); pushWatched(); }
        return;
      }
      paperSizes.set(mint, sizing.sizeSol);
      if (realistic) void measureLiveCost(mint, realistic.slot1Buyers).catch((err) => log.debug('live cost measure failed', { mint, err }));
      const maxHorizonMs = Math.max(0, ...rc.paperHorizonsSec) * 1_000;
      const pathWaitMs = Math.max(0, entryAtMs + maxHorizonMs + 10_000 - Date.now());
      setTimeout(() => {
        try {
          const captured = pathRecorder.finish(mint, tx.slot!, tx.signature, entry.poolBase, entry.poolQuoteLamports, virtualQuote!);
          pathCtx.delete(mint);
          pushWatched();
          const mid = realistic?.entries.slot1mid;
          const pathSizeSol = paperSizes.get(mint) ?? rc.paperSizeSol;
          const roundTrip = measuredPathCosts.get(mint) ?? rc.liveCostSol;
          // Exit-failure prior for the FSM legs: trailing land rate of an
          // ordinary (uncontested slot-1) send, refreshed at finish time.
          let exitP = 1;
          let failFeeSol = 0;
          try {
            const stats = repos.trailingFillStats(rc.fillLookbackHours);
            exitP = rateFor(stats, 1, false) ?? 1;
            failFeeSol = stats.meanFailedFeeSol ?? 0;
          } catch (err) {
            log.debug('fsm exit prior unavailable — sends land', { mint, err });
          }
          const results = Object.fromEntries(EXIT_RULES.map((rule) => {
            if (!mid || realistic?.quality !== 'verified') return [rule, { reason: 'unpriced-exit', pnlPct: null, exitAtMs: null }];
            if (rule === 'live-fsm') {
              // Baseline (non-relaxed) barriers; post-hoc tracks carry no
              // volatility flag, so standard trailing gaps. Static: no retune.
              return [rule, simulateFsmExit(mid, captured, entryAtMs, pathSizeSol,
                { buyCostSol: Math.max(0, roundTrip - rc.liveSellCostSol), sellCostSol: rc.liveSellCostSol, failFeeSol },
                exitCfgFor(config, false),
                { maxHoldMs: maxHorizonMs, seed: mint, exitP,
                  maxExitSlippagePct: config.exits.ladderSlippageTiers[config.exits.ladderSlippageTiers.length - 1],
                  emergencySlippagePct: config.exits.emergencySlippagePct })];
            }
            return [rule, simulatePathExit(mid, captured, entryAtMs, pathSizeSol, roundTrip, rule, maxHorizonMs)];
          }));
          repos.recordRacerPath(mint, JSON.stringify({ version: 2, entryAtMs, quality: captured.quality, gapsMs: captured.gapsMs ?? [], states: captured.states, results }), JSON.stringify(results));
          measuredPathCosts.delete(mint);
          paperSizes.delete(mint);
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
      onLaunch: (mint, info) => {
        resolver.noteMint(mint);
        if (info) hotScreen.noteLaunch(mint, info.noTransferFee);
      },
      isWatched: (address) => slotEntries.isWatched(address) || pathRecorder.isWatched(address) || probePools.has(address),
      onWatched: (address, data, slot, signature, owner) => {
        const probeMint = probePools.get(address);
        if (probeMint) {
          if (owner === PROGRAM_IDS.PUMP_SWAP) {
            sendReady.onPool(probeMint, { data: Buffer.from(data).toString('base64'), owner, lamports: 0 });
            if (sendReady.awaitingCheck(probeMint)) verifySynth(probeMint);
          }
          return;
        }
        const amount = decodeTokenAmount(data);
        if (amount !== null) slotEntries.onVault(address, amount, slot, signature);
        pathRecorder.onVault(address, amount, slot, signature);
      },
      onCurve: (u) =>
        resolver.resolve(u.curve, u.signature, u, (mint, upd) => {
          if (upd.creator) {
            creators.set(mint, upd.creator);
            hotScreen.noteCreator(mint, upd.creator);
          }
          if (creators.size > 20_000) creators.clear();
          tracker.onCurve(mint, upd.curve, upd.reserves, upd.slot);
        }),
      onMigrateTx: (tx, receivedAtNs) => {
        // Hot path first: nothing (DB, logs, lookups) runs before the build.
        if (rc.sendReadyProbeEnabled && tx.landed && tx.mint && !migrated.has(tx.mint) && receivedAtNs !== undefined) {
          const mint = tx.mint;
          const prep = sendReady.isPrepared(mint);
          let mintFromTx = false;
          if (!prep && tx.tokenProgram) {
            // Admitted too late (same-slot complete → migrate) or the read failed:
            // derive what the buy needs from the migrate tx itself — no RPC.
            try {
              const poolKey = canonicalPumpPoolPda(new PublicKey(mint)).toBase58();
              probePools.set(poolKey, mint);
              void sendReady.prepare(
                mint,
                {
                  poolKey,
                  baseMint: pumpMintAccount(tx.tokenProgram),
                  userBaseAta: deriveAta(probeWallet.publicKey, mint, tx.tokenProgram === PROGRAM_IDS.TOKEN_2022),
                  userQuoteAta,
                },
                creators.get(mint) ?? null,
              );
              mintFromTx = true;
            } catch (err) {
              log.debug('migrate-time prep failed', { mint, err });
            }
          }
          let base = 0n;
          let quote = 0n;
          for (const b of tx.post) {
            if (!b.owner || !probePools.has(b.owner)) continue;
            if (b.mint === mint) base += b.amount;
            else if (b.mint === WSOL_MINT) quote += b.amount;
          }
          const receiptSlot = stream.heartbeatSlot;
          const migrateSlot = tx.slot;
          let poolForScreen = '';
          for (const [pool, m] of probePools) if (m === mint) poolForScreen = pool;
          const probed = sendReady.probe({
            mint,
            receivedAtNs,
            baseReserve: base,
            quoteReserveLamports: quote,
            screen: ({ virtualQuoteLamports }) => {
              const r = hotScreen.screen({
                mint,
                poolAddress: poolForScreen,
                migrateSlot: tx.slot,
                vaultQuoteLamports: quote,
                virtualQuoteLamports,
                isToken2022Fallback: tx.tokenProgram === PROGRAM_IDS.TOKEN_2022,
              });
              hotVerdicts.set(mint, r);
              if (hotVerdicts.size > 5_000) hotVerdicts.clear();
              return r.accept;
            },
          });
          // Earliest landing slot: the slot in progress when a send leaving at
          // send-ready (+ one-way allowance) would reach the leader.
          landing.set(
            mint,
            probed
              .then((r): LandingTiming => {
                if (!r.ok || migrateSlot === null) return { receiptSlot, sendReadyUs: null, feasibleSlot: null, feasibleSlotRaw: null, landingWeights: null };
                const tNs = receivedAtNs + BigInt(Math.round(r.totalUs * 1_000 + rc.sendOneWayMs * 1_000_000));
                const raw = stream.slotInProgressAt(tNs);
                // Landing distribution: push the trailing pipeline samples
                // through the slot boundaries from this receipt. Null (fresh
                // DB, unmappable slots) falls back to the point feasible leg.
                let landingWeights: LandingWeights | null = null;
                try {
                  const oneWayNs = BigInt(Math.round(rc.sendOneWayMs * 1_000_000));
                  landingWeights = tallyLandingOffsets(
                    repos.trailingSendReadyUs(rc.landingLookbackHours).map((us) => {
                      const slot = stream.slotInProgressAt(receivedAtNs + BigInt(Math.round(us * 1_000)) + oneWayNs);
                      return slot === null ? null : slot - migrateSlot;
                    }),
                  );
                } catch (err) {
                  log.debug('landing distribution unavailable — point feasible leg', { mint, err });
                }
                return { receiptSlot, sendReadyUs: r.totalUs, feasibleSlotRaw: raw, feasibleSlot: raw === null ? null : Math.max(migrateSlot + 1, raw), landingWeights };
              })
              .catch((): LandingTiming => ({ receiptSlot, sendReadyUs: null, feasibleSlot: null, feasibleSlotRaw: null, landingWeights: null })),
          );
          void probed
            .then((r) => {
              if (r.ok && r.poolSource === 'synthesized') {
                // Keep watching the pool: its push audits the synthesized build.
                if (sendReady.awaitingCheck(mint) && probePools.size) {
                  const t = setTimeout(() => {
                    if (sendReady.awaitingCheck(mint)) {
                      synthStats.unverified++;
                      releaseProbe(mint);
                      pushWatched();
                    }
                  }, 5_000);
                  t.unref?.();
                }
                verifySynth(mint); // the push may already be here
              } else {
                // The migrated pool's own account is no longer needed: stop watching it.
                releaseProbe(mint);
                pushWatched();
              }
              if (r.ok && mintFromTx) {
                // Audit the tx-derived mint against the real account, off the hot path.
                void rpc
                  .getAccountInfoBase64(mint, 'confirmed')
                  .then((real) => {
                    if (!real) return;
                    const info = decodeMint(real.data, real.owner);
                    const match = real.owner === tx.tokenProgram && info.decimals === 6;
                    repos.recordLatencySample({ kind: 'racer_mint_synth_match', latencyMs: match ? 1 : 0, mint, feedSource: 'racer-probe' });
                    repos.recordLatencySample({ kind: 'racer_mint_supply_delta', latencyMs: Number(PUMP_MINT_SUPPLY - info.supply), mint, feedSource: 'racer-probe' });
                    if (!match) log.warn('tx-derived mint differs from the real mint', { mint, owner: real.owner, decimals: info.decimals });
                  })
                  .catch(() => undefined);
              }
              const hv = hotVerdicts.get(mint);
              const screenUs = r.screenUs ?? null;
              if (screenUs != null && hv) {
                repos.recordLatencySample({ kind: 'racer_send_screen_us', latencyMs: screenUs, mint, feedSource: 'racer-probe' });
                repos.recordLatencySample({ kind: 'racer_screen_accept', latencyMs: hv.accept ? 1 : 0, mint, feedSource: 'racer-probe' });
                repos.recordLatencySample({ kind: 'racer_screen_prepared', latencyMs: hv.prepared ? 1 : 0, mint, feedSource: 'racer-probe' });
              }
              if (r.ok) {
                sendReadyStats.ok++;
                for (const [kind, field] of SEND_KINDS) repos.recordLatencySample({ kind, latencyMs: r[field], mint, feedSource: 'racer-probe' });
                repos.recordLatencySample({ kind: 'racer_send_pool_synth', latencyMs: r.poolSource === 'synthesized' ? 1 : 0, mint, feedSource: 'racer-probe' });
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
    // Seed keep-warm (and the synthesis template) from the latest recorded
    // canonical pool — or, on a fresh DB, a known one — so even the first live
    // probe after a restart runs on hot code; real probes then refresh the inputs.
    void (async () => {
      const recorded = db
        .prepare(`SELECT mint, pool_address AS pool FROM racer_migrations
                  WHERE pool_address IS NOT NULL AND unpriced_reason IS NULL ORDER BY rowid DESC LIMIT 1`)
        .get() as { mint: string; pool: string } | undefined;
      let last: { mint: string; pool: string } | null = null;
      type Acct = Awaited<ReturnType<typeof rpc.getAccountInfoBase64>>;
      let poolAcct = null as Acct;
      let mintAcct = null as Acct;
      for (const cand of [recorded, BOOT_SEED_POOL]) {
        if (!cand) continue;
        for (let attempt = 0; attempt < 3 && !poolAcct; attempt++) {
          if (attempt > 0) await new Promise((r) => setTimeout(r, 2_000));
          [poolAcct, mintAcct] = await rpc.getMultipleAccountsBase64([cand.pool, cand.mint], 'confirmed').catch(() => [null, null]);
        }
        if (poolAcct && mintAcct && poolAcct.owner === PROGRAM_IDS.PUMP_SWAP) {
          last = cand;
          break;
        }
        poolAcct = mintAcct = null;
      }
      if (!last || !poolAcct || !mintAcct) {
        log.warn('send-ready warm seed: no pool readable; first probes run cold');
        return;
      }
      log.info('send-ready warm seeded', { pool: last.pool, source: last === recorded ? 'db' : 'boot-seed' });
      if (!sendReady.hasTemplate) sendReady.setPoolTemplate(poolAcct); // a real pool may have arrived meanwhile
      sendReady.seedWarm(
        {
          poolKey: last.pool,
          baseMint: mintAcct,
          userBaseAta: deriveAta(probeWallet.publicKey, last.mint, mintAcct.owner === PROGRAM_IDS.TOKEN_2022),
          userQuoteAta,
        },
        poolAcct,
        206_900_000_000_000n,
        84_990_000_000n,
      );
    })().catch((err) => log.debug('send-ready warm seed failed', { err }));
    if (rc.sendReadyWarmMs > 0) {
      const warmTimer = setInterval(() => void sendReady.warm(), rc.sendReadyWarmMs);
      warmTimer.unref?.();
    }
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
      hotScreen: hotScreen.stats,
      sendReady: { ...sendReadyStats, prepared: probePools.size, warmRuns: sendReady.warmRuns, template: sendReady.hasTemplate, synth: synthStats },
      horizonExits: pathExitStats,
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
