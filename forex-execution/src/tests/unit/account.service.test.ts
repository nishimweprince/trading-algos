import { describe, expect, it } from 'vitest';
import { loadConfig } from '../../config/env.js';
import { AccountService } from '../../oanda/account.service.js';
import { OandaClient } from '../../oanda/oanda-client.js';

const config = loadConfig({
  INTERNAL_API_KEY: 'replace-with-a-long-random-value',
  OANDA_ACCOUNT_ID: '101-001-123456-001',
  OANDA_API_TOKEN: 'secret-token',
  DATABASE_URL: 'postgresql://postgres:postgres@localhost:5432/oanda_trading',
  REDIS_URL: 'redis://localhost:6379',
});

class FakeClient implements OandaClient {
  get<T>(path: string): Promise<T> {
    if (path.endsWith('/summary')) {
      return Promise.resolve({
        account: {
          id: '101-001-123456-001', currency: 'USD', balance: '1000', NAV: '1005', marginAvailable: '900', marginUsed: '100', unrealizedPL: '5', pl: '10', openTradeCount: 1, openPositionCount: 1, pendingOrderCount: 0, lastTransactionID: '12', hedgingEnabled: false,
        },
        lastTransactionID: '12',
      } as T);
    }
    if (path.endsWith('/instruments')) {
      return Promise.resolve({
        instruments: [{ name: 'EUR_USD', type: 'CURRENCY', displayName: 'EUR/USD', pipLocation: -4, displayPrecision: 5, tradeUnitsPrecision: 0, minimumTradeSize: '1', maximumOrderUnits: '100000000', marginRate: '0.02' }],
        lastTransactionID: '12',
      } as T);
    }
    return Promise.resolve({ changes: {}, state: {}, lastTransactionID: '13' } as T);
  }
  post<T>(): Promise<T> { return Promise.reject(new Error('not used')); }
  put<T>(): Promise<T> { return Promise.reject(new Error('not used')); }
  patch<T>(): Promise<T> { return Promise.reject(new Error('not used')); }
  delete<T>(): Promise<T> { return Promise.reject(new Error('not used')); }
}

describe('AccountService', () => {
  it('normalizes account summary fields', async () => {
    const service = new AccountService(new FakeClient(), config);
    await expect(service.getSummary()).resolves.toMatchObject({ accountId: '101-001-123456-001', nav: '1005', lastTransactionId: '12' });
  });

  it('normalizes tradable instrument metadata', async () => {
    const service = new AccountService(new FakeClient(), config);
    await expect(service.listInstruments()).resolves.toEqual([expect.objectContaining({ name: 'EUR_USD', pipLocation: -4, displayPrecision: 5 })]);
  });
});
