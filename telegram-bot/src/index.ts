import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadConfig, type AppConfig } from './config.js';
import { createLogger } from './logger.js';
import { createJsonlWriter } from './state/jsonlLog.js';
import { readCursors, writeCursorsAtomic } from './state/cursorStore.js';
import { readSessionString, createTelegramClient } from './telegram/client.js';
import { clearChannelResolverCache } from './telegram/channelResolver.js';
import { Deduper } from './signals/deduper.js';
import { PindoClient } from './notifications/pindoClient.js';
import { createApp, listenApp } from './http/server.js';
import type { StatusSnapshot } from './http/routes/health.js';
import { pollLoopTicks, createShutdownController } from './scheduler/pollLoop.js';
import { processChannelTick, resolveAllChannels } from './polling/tick.js';
import { toIso } from './util/time.js';
import { sleepMs } from './util/retry.js';

const cwd = process.cwd();

async function ensureDir(dir: string): Promise<void> {
  await fs.mkdir(dir, { recursive: true });
}

async function main(): Promise<void> {
  let config: AppConfig;
  try {
    config = loadConfig(cwd);
  } catch (e) {
    console.error(e instanceof Error ? e.message : e);
    process.exit(1);
    return;
  }

  const logger = createLogger(config);
  await ensureDir(config.resolvedStateDir);
  await ensureDir(config.resolvedLogsDir);

  const signalsLog = createJsonlWriter(path.join(config.resolvedLogsDir, 'signals.jsonl'));
  const smsLog = createJsonlWriter(path.join(config.resolvedLogsDir, 'sms.jsonl'));
  const errorsLog = createJsonlWriter(path.join(config.resolvedLogsDir, 'errors.jsonl'));
  const rawLog = createJsonlWriter(path.join(config.resolvedLogsDir, 'raw.jsonl'));

  const session = await readSessionString(config.resolvedSessionPath);
  if (!session) {
    logger.error(
      { path: config.resolvedSessionPath },
      'Missing or empty Telegram session. Run `npm run login` first.',
    );
    process.exit(1);
    return;
  }

  const client = createTelegramClient(session, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH);
  await client.connect();
  if (!(await client.checkAuthorization())) {
    logger.error('Telegram session not authorized. Re-run `npm run login`.');
    await client.disconnect();
    process.exit(1);
    return;
  }

  const cursors = await readCursors(config.resolvedCursorsPath);
  const deduper = await Deduper.load(config.resolvedHandledPath);
  const pindo = new PindoClient(config);

  const counters = {
    messagesPolledTotal: 0,
    signalsDetectedTotal: 0,
    smsAttemptsTotal: 0,
    smsFailuresTotal: 0,
  };

  const status: StatusSnapshot = {
    startedAt: Date.now(),
    telegramConnected: true,
    lastTickAt: null,
    channels: [],
    counters,
  };

  const notifyDeps = {
    config,
    logger,
    pindo,
    smsLog,
    errorsLog,
    deduper,
    handledPath: config.resolvedHandledPath,
    onSmsAttempt: () => {
      counters.smsAttemptsTotal += 1;
    },
    onSmsFailure: () => {
      counters.smsFailuresTotal += 1;
    },
  };

  const app = createApp(config, logger, status);
  const { close: closeHttp } = await listenApp(app, config.HTTP_HOST, config.HTTP_PORT);
  logger.info({ host: config.HTTP_HOST, port: config.HTTP_PORT }, 'http listening');

  const shutdown = createShutdownController(logger);
  let tickInFlight: Promise<void> = Promise.resolve();

  async function refreshStatusChannels(): Promise<void> {
    status.channels = config.TELEGRAM_CHANNELS.map((ch) => {
      const row = cursors[ch];
      return {
        channel: ch,
        lastSeenMessageId: row?.lastSeenMessageId ?? 0,
        updatedAt: row?.updatedAt ?? '',
      };
    });
  }

  await refreshStatusChannels();

  const intervalMs = config.POLL_INTERVAL_SECONDS * 1000;
  const startTime = Date.now();

  async function runOneTick(): Promise<void> {
    status.lastTickAt = toIso();
    clearChannelResolverCache();
    const resolvedList = await resolveAllChannels(client, config, logger, errorsLog);
    if (resolvedList.length === 0) {
      logger.warn('no channels resolved yet; skipping tick');
      return;
    }
    for (const resolved of resolvedList) {
      await processChannelTick(
        client,
        config,
        logger,
        resolved,
        cursors,
        deduper,
        signalsLog,
        errorsLog,
        rawLog,
        notifyDeps,
        counters,
      );
      await refreshStatusChannels();
    }
    await writeCursorsAtomic(config.resolvedCursorsPath, cursors);
    await Deduper.persistAtomic(config.resolvedHandledPath, deduper.toFile());
  }

  (async () => {
    try {
      for await (const _tick of pollLoopTicks(intervalMs, startTime, shutdown.shouldStop)) {
        if (shutdown.shouldStop()) break;
        tickInFlight = (async () => {
          try {
            await runOneTick();
          } catch (e) {
            logger.error({ err: e }, 'poll tick failed');
            errorsLog.append({
              ts: toIso(),
              kind: 'tick_error',
              details: { error: e instanceof Error ? e.message : String(e) },
            });
            await errorsLog.flush();
          }
        })();
        await tickInFlight;
      }
    } catch (e) {
      logger.error({ err: e }, 'poll loop exited with error');
    }
  })();

  async function gracefulShutdown(): Promise<void> {
    shutdown.dispose();
    await Promise.race([tickInFlight, sleepMs(10_000)]);
    try {
      await writeCursorsAtomic(config.resolvedCursorsPath, cursors);
      await Deduper.persistAtomic(config.resolvedHandledPath, deduper.toFile());
    } catch (e) {
      logger.error({ err: e }, 'persist on shutdown failed');
    }
    status.telegramConnected = false;
    await client.disconnect();
    await closeHttp();
    await signalsLog.flush();
    await smsLog.flush();
    await errorsLog.flush();
    await rawLog.flush();
    logger.info('shutdown complete');
  }

  process.once('SIGINT', () => void gracefulShutdown().then(() => process.exit(0)));
  process.once('SIGTERM', () => void gracefulShutdown().then(() => process.exit(0)));
}

const isMain =
  process.argv[1] &&
  fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);

if (isMain) {
  main().catch((e) => {
    console.error(e);
    process.exit(1);
  });
}
