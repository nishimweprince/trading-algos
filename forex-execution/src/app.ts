import crypto from 'node:crypto';
import Fastify from 'fastify';
import helmet from '@fastify/helmet';
import rateLimit from '@fastify/rate-limit';
import sensible from '@fastify/sensible';
import { AppConfig } from './config/env.js';
import { createLogger } from './common/logging/logger.js';
import { createAuthenticationHook } from './common/http/authentication-hook.js';
import { errorHandler } from './common/http/error-handler.js';
import { AxiosOandaClient } from './oanda/oanda-client.js';
import { AccountService } from './oanda/account.service.js';
import { AccountRepository } from './persistence/repositories/account.repository.js';
import { InstrumentService } from './oanda/instrument.service.js';
import { prisma } from './persistence/prisma.js';
import { registerAccountRoutes } from './routes/account.routes.js';
import { registerHealthRoutes } from './routes/health.routes.js';

export async function buildApp(config: AppConfig) {
  const logger = createLogger(config.LOG_LEVEL, config.APP_NAME);
  const app = Fastify({ loggerInstance: logger, genReqId: () => crypto.randomUUID() });
  await app.register(helmet);
  await app.register(rateLimit, { max: 300, timeWindow: '1 minute' });
  await app.register(sensible);
  app.addHook('preHandler', createAuthenticationHook(config));
  app.setErrorHandler(errorHandler);
  const oandaClient = new AxiosOandaClient(config, logger);
  const accountRepository = new AccountRepository(prisma, config);
  const accountService = new AccountService(oandaClient, config, accountRepository);
  const instrumentService = new InstrumentService(accountService);
  registerHealthRoutes(app, accountService, instrumentService);
  registerAccountRoutes(app, accountService, instrumentService, accountRepository);
  return app;
}
