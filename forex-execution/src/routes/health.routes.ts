import {
  FastifyBaseLogger,
  FastifyInstance,
  RawReplyDefaultExpression,
  RawRequestDefaultExpression,
  RawServerDefault,
} from 'fastify';
import { AccountService } from '../oanda/account.service.js';
import { InstrumentService } from '../oanda/instrument.service.js';

type RouteApp<Logger extends FastifyBaseLogger> = FastifyInstance<
  RawServerDefault,
  RawRequestDefaultExpression,
  RawReplyDefaultExpression,
  Logger
>;

export function registerHealthRoutes<Logger extends FastifyBaseLogger>(
  app: RouteApp<Logger>,
  accountService: AccountService,
  instrumentService: InstrumentService,
): void {
  app.get('/health/live', () => ({ status: 'ok' }));
  app.get('/health/ready', async () => {
    const summary = await accountService.getSummary();
    const instruments = await instrumentService.list();
    return {
      status: 'ready',
      checks: { config: 'ok', oandaAuthentication: 'ok', instrumentMetadata: instruments.length > 0 ? 'ok' : 'empty' },
      account: { id: summary.accountId, currency: summary.currency },
    };
  });
}
