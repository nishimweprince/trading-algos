import { loadConfig, readSecret, ConfigError } from './config/load.ts';
import { acquireLock, LockError, type InstanceLock } from './core/lock.ts';
import { TypedBus } from './core/bus.ts';
import { logger, registerSecret } from './core/logger.ts';
import { openDb, prunePriceTicks, type DB } from './persistence/db.ts';
import { Repositories } from './persistence/repositories.ts';
import { Alerter } from './alerts/telegram.ts';
import { RpcClient } from './core/rpc.ts';
import { assertProgramsExist } from './core/programs.ts';
import { Detector } from './detector/index.ts';
import { GuardrailPipeline } from './guardrails/pipeline.ts';
import { PricePoller } from './positions/pricing.ts';
import { PositionManager } from './positions/manager.ts';
import { Executor } from './executor/index.ts';
import { SellabilitySimulator } from './executor/sellability.ts';
import { RiskManager } from './risk/manager.ts';
import { KillFileWatcher } from './risk/killswitch.ts';
import { startDashboardServer, type DashboardRuntime } from './dashboard/server.ts';

/**
 * Bootstrap (Section 3.1 / Phase 0). Responsibilities:
 *   1. Load + validate config (zod).
 *   2. Acquire the single-instance lock (no double-trading).
 *   3. Register secrets for log redaction.
 *   4. Open SQLite, build the event bus, wire Telegram.
 *   5. Record a dashboard startup notification.
 *   6. Install graceful-shutdown handlers.
 *
 * Detection, guardrails, execution, and risk management land in later phases;
 * this file is the stable spine they plug into.
 */

interface Runtime {
  lock: InstanceLock;
  db: DB;
  bus: TypedBus;
  alerter: Alerter;
  detector: Detector;
  guardrails: GuardrailPipeline | null;
  positions: PositionManager | null;
  risk: RiskManager;
  killWatcher: KillFileWatcher;
  dashboard: DashboardRuntime | null;
  maintenance: NodeJS.Timeout;
}

const MAINTENANCE_INTERVAL_MS = 60 * 60 * 1000; // hourly

async function main(): Promise<void> {
  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  const log = logger.child({ mod: 'bootstrap' });

  // Redact the wallet secret from all logs before anything else runs.
  registerSecret(readSecret(config.wallet.keypairEnvVar));

  log.info('config loaded', {
    mode: config.mode,
    dbPath: config.persistence.dbPath,
    maxConcurrent: config.risk.maxConcurrentPositions,
  });

  if (config.mode === 'live') {
    log.warn('LIVE MODE — real capital is at risk. Circuit breakers are the only safety net.');
    if (!readSecret(config.wallet.keypairEnvVar)) {
      throw new ConfigError(`live mode requires ${config.wallet.keypairEnvVar} to be set`);
    }
  }

  const lock = acquireLock();
  const db = openDb({ path: config.persistence.dbPath });
  const bus = new TypedBus();
  const alerter = Alerter.create(config);
  alerter.attach(bus);

  const repos = new Repositories(db);
  // Prove persistence is writable before we announce readiness.
  repos.countGraduations();

  // On-chain confirmation/enrichment client. Optional — the detector records
  // graduations unconfirmed when absent (free-tier bootstrap).
  const rpc = config.rpc?.primaryHttp
    ? new RpcClient({ httpUrl: config.rpc.primaryHttp, maxConcurrent: config.rpc.maxConcurrentRequests })
    : undefined;
  if (!rpc) {
    log.warn('no rpc.primaryHttp configured — detection will run without on-chain confirmation');
  }

  // Refuse to start blind: verify pinned/overridden program IDs exist on-chain.
  if (rpc && config.assertProgramIdsOnChain) {
    await assertProgramsExist(rpc, config);
    log.info('program-ID on-chain assertion passed');
  }

  const detector = new Detector(rpc ? { config, bus, repos, rpc } : { config, bus, repos });

  // Executor (dry-run/live only): builds + broadcasts real swaps. Paper never
  // constructs it, so no wallet/tx path is touched in paper mode.
  const executor =
    rpc && config.rpc?.primaryHttp && config.mode !== 'paper'
      ? new Executor({ config, rpc, httpUrl: config.rpc.primaryHttp })
      : undefined;

  // Risk manager + circuit breakers. Wallet-floor / pct-of-wallet checks need a
  // balance provider — only in dry-run/live where a wallet exists.
  const riskManager = new RiskManager({
    config,
    bus,
    repos,
    ...(rpc && executor ? { getWalletBalanceLamports: () => rpc.getBalance(executor.publicKey) } : {}),
  });

  // H4 sellability probe: atomic buy+sell simulation. Dry-run/live only (needs a
  // wallet); a funded wallet is required for a conclusive pass/fail.
  const sellability =
    rpc && config.rpc?.primaryHttp && config.mode !== 'paper'
      ? new SellabilitySimulator({ httpUrl: config.rpc.primaryHttp, config })
      : undefined;

  // Guardrail screening needs on-chain reads; only runs when an RPC is present.
  const guardrails = rpc
    ? new GuardrailPipeline({ config, bus, repos, rpc, risk: riskManager, ...(sellability ? { sellability } : {}) })
    : null;
  if (!guardrails) {
    log.warn('no rpc — guardrail screening disabled; graduations will be logged but not screened');
  }

  // Positions: local pricing + exit FSM (paper accounting in all modes). Also
  // needs RPC (vault polling).
  const positions = rpc
    ? new PositionManager({
        config,
        bus,
        repos,
        poller: new PricePoller(rpc, config.positions.pricePollMs),
        risk: riskManager,
        ...(executor ? { executor } : {}),
      })
    : null;

  const dashboard = startDashboardServer({ config, db, bus, repos });

  // Kill switch: file sentinel + admin Telegram commands.
  const killWatcher = new KillFileWatcher({ bus });
  alerter.startCommands({
    bus,
    adminUserIds: config.alerts.adminUserIds,
    getStatus: () => `${riskManager.statusSummary()} | open ${positions?.openCount ?? 0}`,
  });

  // Hourly maintenance: enforce price-tick retention. Also keeps the event loop
  // alive so the process runs as a daemon until signalled (later phases add the
  // detection streams that keep it busy).
  const maintenance = setInterval(() => {
    try {
      const removed = prunePriceTicks(db, config.persistence.priceTickRetentionDays);
      if (removed > 0) log.debug('pruned old price ticks', { removed });
    } catch (err) {
      log.error('maintenance tick failed', { err });
    }
  }, MAINTENANCE_INTERVAL_MS);

  const runtime: Runtime = {
    lock, db, bus, alerter, detector, guardrails, positions, risk: riskManager, killWatcher, dashboard, maintenance,
  };
  installShutdown(runtime, log);

  alerter.startupMessage(config, bus);

  // Order matters: risk manager must listen before positions close (breaker
  // counters), positions before screening emits openPosition, screening before
  // detection emits graduations.
  riskManager.start();
  positions?.start();
  await positions?.recoverExitingPositions();
  await positions?.recoverOpenPositions();
  guardrails?.start();
  killWatcher.start();
  await detector.start();

  log.info('boot complete — detect → screen → positions (risk-gated)', {
    mode: config.mode,
    onChainConfirmation: Boolean(rpc),
    screening: Boolean(guardrails),
    paperPositions: Boolean(positions),
  });
}

function installShutdown(rt: Runtime, log: ReturnType<typeof logger.child>): void {
  let shuttingDown = false;
  const shutdown = async (signal: string) => {
    if (shuttingDown) return;
    shuttingDown = true;
    log.info('shutting down', { signal });
    clearInterval(rt.maintenance);
    rt.killWatcher.stop();
    rt.guardrails?.stop();
    rt.positions?.stop();
    rt.risk.stop();
    try {
      await rt.dashboard?.stop();
    } catch (err) {
      log.error('dashboard stop failed', { err });
    }
    try {
      await rt.detector.stop();
    } catch (err) {
      log.error('detector stop failed', { err });
    }
    try {
      await rt.alerter.stop();
    } catch (err) {
      log.error('alerter stop failed', { err });
    }
    try {
      rt.db.close();
    } catch (err) {
      log.error('db close failed', { err });
    }
    rt.lock.release();
    log.info('shutdown complete');
    process.exit(0);
  };

  process.on('SIGINT', () => void shutdown('SIGINT'));
  process.on('SIGTERM', () => void shutdown('SIGTERM'));
  process.on('uncaughtException', (err) => {
    log.error('uncaught exception', { err });
    void shutdown('uncaughtException');
  });
  process.on('unhandledRejection', (reason) => {
    log.error('unhandled rejection', { err: reason });
  });
}

main().catch((err) => {
  if (err instanceof ConfigError || err instanceof LockError) {
    // Expected, actionable startup failures — no stack spam.
    logger.error(err.message, { kind: err.name });
  } else {
    logger.error('fatal boot error', { err });
  }
  process.exit(1);
});
