import {
  FastifyBaseLogger,
  FastifyInstance,
  RawReplyDefaultExpression,
  RawRequestDefaultExpression,
  RawServerDefault,
} from 'fastify';
import { z } from 'zod';
import { AccountService } from '../oanda/account.service.js';
import { InstrumentService } from '../oanda/instrument.service.js';
import { AccountRepository } from '../persistence/repositories/account.repository.js';

const changesQuerySchema = z.object({ sinceTransactionId: z.string().min(1) });
const snapshotsQuerySchema = z.object({ limit: z.coerce.number().int().positive().max(100).default(25) });

type RouteApp<Logger extends FastifyBaseLogger> = FastifyInstance<
  RawServerDefault,
  RawRequestDefaultExpression,
  RawReplyDefaultExpression,
  Logger
>;

export function registerAccountRoutes<Logger extends FastifyBaseLogger>(
  app: RouteApp<Logger>,
  accountService: AccountService,
  instrumentService: InstrumentService,
  accountRepository: AccountRepository,
): void {
  app.get('/api/v1/account/summary', () => accountService.getSummary());
  app.get('/api/v1/account/details', () => accountService.getDetails());
  app.get('/api/v1/account/instruments', () => instrumentService.list());
  app.get('/api/v1/account/snapshots', (request) => {
    const query = snapshotsQuerySchema.parse(request.query);
    return accountRepository.listSnapshots(query.limit);
  });
  app.get('/api/v1/account/changes', (request) => {
    const query = changesQuerySchema.parse(request.query);
    return accountService.getChangesSince(query.sinceTransactionId);
  });
}
