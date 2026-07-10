import { FastifyInstance } from 'fastify';
import { AccountService } from '../oanda/account.service.js';

export async function registerHealthRoutes(app: FastifyInstance, accountService: AccountService): Promise<void> {
  app.get('/health/live', async () => ({ status: 'ok' }));
  app.get('/health/ready', async () => {
    const summary = await accountService.getSummary();
    return { status: 'ready', checks: { config: 'ok', oandaAuthentication: 'ok' }, account: { id: summary.accountId, currency: summary.currency } };
  });
}
