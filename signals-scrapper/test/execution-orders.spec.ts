import { mkdtempSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { AppConfigService } from '../src/config/app-config.service';
import { assertRuntimeConfig } from '../src/config/sources.schema';
import { DedupService } from '../src/dedup/dedup.service';
import { TradingIdea } from '../src/models/trading-idea.model';
import { Mt5ExecutionService } from '../src/mt5/mt5-execution.service';

/** EXECUTION_API=orders: the broker-neutral execution-service API (cTrader production). */

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function idea(provider: TradingIdea['provider'] = 'TRADING_CENTRAL'): TradingIdea {
  return {
    provider,
    instrument: 'XAU/USD',
    timeframe: '30 MIN',
    direction: 'UP',
    stopLoss: 3980,
    takeProfit: 4040,
    entry: 4000,
    ideaTimestamp: '2026-07-14T14:58:00.000Z',
    capturedAt: '2026-07-14T15:00:00.000Z',
    sourceUrl: 'https://example.com/tc',
    hash: 'e'.repeat(64),
  };
}

function operation(id: string, state: string, target: Record<string, unknown> = {}) {
  return {
    operation_id: id,
    action: 'place_order',
    state,
    targets: [{ account: 'icm_demo', state: 'filled', position_id: 77, ...target }],
    created_at: '2026-07-14T15:00:01Z',
    updated_at: '2026-07-14T15:00:01Z',
  };
}

describe('execution outbox, EXECUTION_API=orders', () => {
  let dir: string;
  let config: AppConfigService;
  let dedup: DedupService;
  let service: Mt5ExecutionService;
  let signalId: string;

  function setup(provider: TradingIdea['provider'] = 'TRADING_CENTRAL') {
    service = new Mt5ExecutionService(config, dedup);
    const record = service.createExecutionRecords([idea(provider)])[0];
    signalId = record.signalId;
    dedup.markSeen([idea(provider)], [record]);
  }

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'exec-orders-'));
    config = new AppConfigService();
    config.load({
      SOURCES: JSON.stringify([{ type: 'TRADING_CENTRAL', url: 'https://example.com/tc' }]),
      SEEN_STATE_PATH: join(dir, 'seen.json'),
      MT5_SIGNAL_TRADING_ENABLED: 'true',
      MT5_SIGNAL_API_URL: 'http://127.0.0.1:8010',
      MT5_SIGNAL_API_KEY: 'test-api-key-value',
      MT5_SIGNAL_TIMEOUT_MS: '25',
      MT5_SIGNAL_RULES: JSON.stringify({ 'XAU/USD': { symbol: 'XAUUSD', volume: '0.10' } }),
      EXECUTION_API: 'orders',
      EXECUTION_ACCOUNT: 'icm_demo',
    });
    dedup = new DedupService(config);
    dedup.onModuleInit();
  });

  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  it('posts a broker-neutral OrderRequest keyed by the signal id', async () => {
    setup();
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready' }))
      .mockResolvedValueOnce(jsonResponse(operation(signalId, 'succeeded'), 201));
    service.setFetchImplementation(fetchMock);

    const summary = await service.processOutbox();

    expect(fetchMock.mock.calls[0][0]).toBe('http://127.0.0.1:8010/health/trading-ready');
    expect((fetchMock.mock.calls[0][1] as RequestInit).headers).toMatchObject({
      'X-API-Key': 'test-api-key-value',
    });
    expect(fetchMock.mock.calls[1][0]).toBe('http://127.0.0.1:8010/v1/orders');
    const body = JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body));
    expect(body).toEqual({
      operation_id: signalId,
      occurred_at: '2026-07-14T15:00:00.000Z',
      source: 'trading_central',
      instrument: 'XAUUSD',
      execution_type: 'market',
      direction: 'buy',
      targets: [{ account: 'icm_demo', volume_lots: '0.10' }],
      stop_loss: '3980',
      take_profit: '4040',
      note: `signals-scrapper ${'e'.repeat(64)}`,
    });
    expect(summary.succeeded).toBe(1);
    expect(dedup.listExecutions()[0].status).toBe('succeeded');
  });

  it('records a broker rejection with its error code', async () => {
    setup();
    service.setFetchImplementation(
      jest
        .fn()
        .mockResolvedValueOnce(jsonResponse({ status: 'ready' }))
        .mockResolvedValueOnce(
          jsonResponse(
            operation(signalId, 'rejected', {
              state: 'rejected',
              error_code: 'MARKET_CLOSED',
              error_message: 'Market is closed',
            }),
            201,
          ),
        ),
    );

    await service.processOutbox();

    const record = dedup.listExecutions()[0];
    expect(record.status).toBe('rejected');
    expect(record.error).toMatchObject({ code: 'MARKET_CLOSED' });
  });

  it('keeps a 202 pending operation and settles it from /v1/operations', async () => {
    setup();
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready' }))
      .mockResolvedValueOnce(jsonResponse(operation(signalId, 'pending'), 202))
      .mockResolvedValueOnce(jsonResponse(operation(signalId, 'succeeded')));
    service.setFetchImplementation(fetchMock);

    await service.processOutbox();
    expect(dedup.listExecutions()[0].status).toBe('submitting');

    await service.processOutbox();
    expect(fetchMock.mock.calls[2][0]).toBe(
      `http://127.0.0.1:8010/v1/operations/${signalId}`,
    );
    expect(dedup.listExecutions()[0].status).toBe('succeeded');
  });

  it('a lost submission that the service never saw is retried with the same body', async () => {
    setup();
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready' }))
      .mockRejectedValueOnce(new Error('socket hang up'))
      .mockResolvedValueOnce(jsonResponse({ error: { code: 'operation_not_found', message: 'x' } }, 404))
      .mockResolvedValueOnce(jsonResponse({ status: 'ready' }))
      .mockResolvedValueOnce(jsonResponse(operation(signalId, 'succeeded'), 201));
    service.setFetchImplementation(fetchMock);

    await service.processOutbox();
    expect(dedup.listExecutions()[0].status).toBe('pending');
    await service.processOutbox();

    const first = String((fetchMock.mock.calls[1][1] as RequestInit).body);
    const retry = String((fetchMock.mock.calls[4][1] as RequestInit).body);
    expect(retry).toBe(first);
    expect(dedup.listExecutions()[0].status).toBe('succeeded');
  });

  it('refreshes the Autochartist stop from the execution-service quote', async () => {
    setup('AUTOCHARTIST');
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready' }))
      .mockResolvedValueOnce(jsonResponse({ symbol: 'XAUUSD', bid: 4001, ask: 4001.3 }))
      .mockResolvedValueOnce(jsonResponse(operation(signalId, 'succeeded'), 201));
    service.setFetchImplementation(fetchMock);

    await service.processOutbox();

    expect(fetchMock.mock.calls[1][0]).toBe(
      'http://127.0.0.1:8010/v1/accounts/icm_demo/quote?instrument=XAUUSD',
    );
    const body = JSON.parse(String((fetchMock.mock.calls[2][1] as RequestInit).body));
    expect(body.source).toBe('autochartist');
    expect(Number(body.stop_loss)).toBeLessThan(4001.3);
  });
});

describe('execution outbox, several accounts', () => {
  let dir: string;
  let config: AppConfigService;
  let dedup: DedupService;
  let service: Mt5ExecutionService;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'exec-multi-'));
    config = new AppConfigService();
    config.load({
      SOURCES: JSON.stringify([{ type: 'TRADING_CENTRAL', url: 'https://example.com/tc' }]),
      SEEN_STATE_PATH: join(dir, 'seen.json'),
      MT5_SIGNAL_TRADING_ENABLED: 'true',
      MT5_SIGNAL_API_URL: 'http://127.0.0.1:8010',
      MT5_SIGNAL_API_KEY: 'test-api-key-value',
      MT5_SIGNAL_TIMEOUT_MS: '25',
      MT5_SIGNAL_RULES: JSON.stringify({ 'XAU/USD': { symbol: 'XAUUSD', volume: '0.10' } }),
      EXECUTION_API: 'orders',
      EXECUTION_ACCOUNT: 'icm_demo, ftmo',
    });
    dedup = new DedupService(config);
    dedup.onModuleInit();
    service = new Mt5ExecutionService(config, dedup);
  });

  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  it('sends one order per account; one account failing does not block the other', async () => {
    const records = service.createExecutionRecords([idea()]);
    expect(records.map((r) => r.account)).toEqual(['icm_demo', 'ftmo']);
    expect(new Set(records.map((r) => r.signalId)).size).toBe(2);
    dedup.markSeen([idea()], records);
    const [demoId, ftmoId] = records.map((r) => r.signalId);

    const fetchMock = jest.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/health/trading-ready')) return jsonResponse({ status: 'ready' });
      const body = JSON.parse(String(init?.body));
      if (body.targets[0].account === 'ftmo') {
        return jsonResponse(
          {
            error: {
              code: 'instrument_not_available',
              message: 'Canonical instrument is not configured for a target account',
            },
          },
          422,
        );
      }
      return jsonResponse(operation(body.operation_id, 'succeeded'), 201);
    });
    // a rejected submission is reconciled: the service never recorded it
    const withStatus = jest.fn(async (url: string, init?: RequestInit) =>
      url.includes('/v1/operations/')
        ? jsonResponse({ error: { code: 'operation_not_found', message: 'x' } }, 404)
        : fetchMock(url, init),
    );
    service.setFetchImplementation(withStatus as never);

    await service.processOutbox();

    const bodies = withStatus.mock.calls
      .filter(([url]) => String(url).endsWith('/v1/orders'))
      .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
    expect(bodies.map((b) => [b.operation_id, b.targets])).toEqual([
      [demoId, [{ account: 'icm_demo', volume_lots: '0.10' }]],
      [ftmoId, [{ account: 'ftmo', volume_lots: '0.10' }]],
    ]);
    const byId = Object.fromEntries(dedup.listExecutions().map((r) => [r.signalId, r]));
    expect(byId[demoId].status).toBe('succeeded');
    expect(byId[ftmoId].status).toBe('rejected');
    expect(byId[ftmoId].error).toMatchObject({ code: 'instrument_not_available' });
  });
});

describe('config for orders mode and auto-login', () => {
  const base = {
    SOURCES: JSON.stringify([{ type: 'TRADING_CENTRAL', url: 'https://example.com/tc' }]),
    BROWSER_MODE: 'CDP',
    CHROME_PROFILE: 'DEDICATED',
    CDP_AUTO_START: 'false',
    OPENAI_API_KEY: 'sk-test',
  };

  function load(env: Record<string, string>) {
    const config = new AppConfigService();
    return config.load({ ...base, ...env });
  }

  it('orders mode needs an account alias when trading', () => {
    const trading = {
      MT5_SIGNAL_TRADING_ENABLED: 'true',
      MT5_SIGNAL_API_KEY: 'test-api-key-value',
      MT5_SIGNAL_RULES: JSON.stringify({ 'XAU/USD': { symbol: 'XAUUSD', volume: '0.10' } }),
      EXECUTION_API: 'orders',
    };
    expect(() => assertRuntimeConfig(load(trading))).toThrow(/EXECUTION_ACCOUNT/);
    expect(() =>
      assertRuntimeConfig(load({ ...trading, EXECUTION_ACCOUNT: 'icm_demo' })),
    ).not.toThrow();
  });

  it('auto-login needs email, password and TOTP secret', () => {
    expect(() =>
      assertRuntimeConfig(load({ AUTO_LOGIN_ENABLED: 'true', IC_LOGIN_EMAIL: 'a@b.c' })),
    ).toThrow(/IC_LOGIN_PASSWORD, IC_TOTP_SECRET/);
  });
});
