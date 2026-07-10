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
import { registerHealthRoutes } from './routes/health.routes.js';

export async function buildApp(config: AppConfig) {
  const logger = createLogger(config.LOG_LEVEL, config.APP_NAME);
  const app = Fastify({ logger, genReqId: () => crypto.randomUUID() });
  await app.register(helmet);
  await app.register(rateLimit, { max: 300, timeWindow: '1 minute' });
  await app.register(sensible);
  app.addHook('preHandler', createAuthenticationHook(config));
  app.setErrorHandler(errorHandler);
  const oandaClient = new AxiosOandaClient(config, logger);
  const accountService = new AccountService(oandaClient, config);
  await registerHealthRoutes(app, accountService);
  return app;
}
