import { buildApp } from './app.js';
import { loadConfig, sanitizeConfig } from './config/env.js';
import { createLogger } from './common/logging/logger.js';
import { prisma } from './persistence/prisma.js';

const config = loadConfig();
const bootstrapLogger = createLogger(config.LOG_LEVEL, config.APP_NAME);
const app = await buildApp(config);

const shutdown = async (signal: NodeJS.Signals): Promise<void> => {
  bootstrapLogger.info({ signal }, 'Shutting down');
  await app.close();
  await prisma.$disconnect();
};
process.once('SIGINT', (signal) => { void shutdown(signal); });
process.once('SIGTERM', (signal) => { void shutdown(signal); });

await app.listen({ host: config.HOST, port: config.PORT });
bootstrapLogger.info({ config: sanitizeConfig(config) }, 'OANDA trading server started');
