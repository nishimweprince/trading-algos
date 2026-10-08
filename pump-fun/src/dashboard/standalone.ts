import { serve } from '@hono/node-server';
import { loadConfig } from '../config/load.ts';
import { logger } from '../core/logger.ts';
import { openDb } from '../persistence/db.ts';
import { assertDashboardExposureSafe, createDashboardApp } from './server.ts';
import { Repositories } from '../persistence/repositories.ts';
import type { RiskSnapshot } from '../risk/manager.ts';
import { RISK_SNAPSHOT_KEY, RISK_SNAPSHOT_MAX_AGE_MS } from '../risk/manager.ts';

const log = logger.child({ mod: 'dashboard-standalone' });

async function main(): Promise<void> {
  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  assertDashboardExposureSafe(config);

  const db = openDb({ path: config.persistence.dbPath });
  // The risk manager lives in the trader process; it publishes its snapshot to
  // runtime_state every few seconds. Stale (trader down) reads as offline.
  const repos = new Repositories(db);
  const app = createDashboardApp({
    config,
    db,
    getRiskSnapshot: () => repos.getRuntimeState<RiskSnapshot>(RISK_SNAPSHOT_KEY, RISK_SNAPSHOT_MAX_AGE_MS),
  });
  const server = serve({
    fetch: app.fetch,
    hostname: config.dashboard.host,
    port: config.dashboard.port,
  });

  server.on('listening', () => log.info('dashboard server listening', {
    host: config.dashboard.host,
    port: config.dashboard.port,
    dbPath: config.persistence.dbPath,
  }));

  server.on('error', (err) => {
    log.error('dashboard server failed', { err });
    db.close();
    process.exit(1);
  });

  const stop = (signal: string) => {
    log.info('dashboard server shutting down', { signal });
    server.close(() => {
      db.close();
      log.info('dashboard server stopped');
      process.exit(0);
    });
  };

  process.once('SIGINT', () => stop('SIGINT'));
  process.once('SIGTERM', () => stop('SIGTERM'));
}

main().catch((err) => {
  logger.error('dashboard server failed', { err });
  process.exit(1);
});
