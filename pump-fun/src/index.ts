import { loadConfig, readSecret, ConfigError } from './config/load.ts';
import { acquireLock, LockError, type InstanceLock } from './core/lock.ts';
import { TypedBus } from './core/bus.ts';
import { logger, registerSecret } from './core/logger.ts';
import { openDb, prunePriceTicks, type DB } from './persistence/db.ts';
import { Repositories } from './persistence/repositories.ts';
import { Alerter } from './alerts/telegram.ts';

/**
 * Bootstrap (Section 3.1 / Phase 0). Responsibilities:
 *   1. Load + validate config (zod).
 *   2. Acquire the single-instance lock (no double-trading).
 *   3. Register secrets for log redaction.
 *   4. Open SQLite, build the event bus, wire Telegram.
 *   5. Send a startup alert.
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

  // Prove persistence is writable before we announce readiness.
  new Repositories(db).countGraduations();

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

  const runtime: Runtime = { lock, db, bus, alerter, maintenance };
  installShutdown(runtime, log);

  await alerter.startupMessage(config);

  // Program-ID on-chain assertion (Section 4.1) is deferred to Phase 1, where
  // the RPC client exists. Flag it so the intent is visible in logs today.
  if (config.assertProgramIdsOnChain) {
    log.info('program-id on-chain assertion pending RPC client (Phase 1)');
  }

  log.info('boot complete — idle until detection lands (Phase 1)', { mode: config.mode });
}

function installShutdown(rt: Runtime, log: ReturnType<typeof logger.child>): void {
  let shuttingDown = false;
  const shutdown = async (signal: string) => {
    if (shuttingDown) return;
    shuttingDown = true;
    log.info('shutting down', { signal });
    clearInterval(rt.maintenance);
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
