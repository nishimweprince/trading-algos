import { mkdtempSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { Logger } from '@nestjs/common';
import { AppConfigService } from '../src/config/app-config.service';
import { DedupService } from '../src/dedup/dedup.service';
import { TradingIdea } from '../src/models/trading-idea.model';
import { Mt5ExecutionService } from '../src/mt5/mt5-execution.service';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function idea(): TradingIdea {
  return {
    provider: 'TRADING_CENTRAL',
    instrument: 'EUR/USD',
    timeframe: '30 MIN',
    direction: 'UP',
    stopLoss: 1.08,
    takeProfit: 1.1,
    ideaTimestamp: '2026-07-14T14:58:00.000Z',
    capturedAt: '2026-07-14T15:00:00.000Z',
    sourceUrl: 'https://example.com/tc',
    hash: 'd'.repeat(64),
  };
}

describe('Mt5ExecutionService', () => {
  let dir: string;
  let config: AppConfigService;
  let dedup: DedupService;
  let service: Mt5ExecutionService;
  let signalId: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'mt5-execution-'));
    config = new AppConfigService();
    config.load({
      SOURCES: JSON.stringify([
        { type: 'TRADING_CENTRAL', url: 'https://example.com/tc' },
      ]),
      SEEN_STATE_PATH: join(dir, 'seen.json'),
      MT5_SIGNAL_TRADING_ENABLED: 'true',
      MT5_SIGNAL_API_URL: 'http://127.0.0.1:8000',
      MT5_SIGNAL_API_KEY: 'test-api-key-value',
      MT5_SIGNAL_TIMEOUT_MS: '25',
      MT5_SIGNAL_RULES: JSON.stringify({
        'EUR/USD': { symbol: 'EURUSD', volume: '0.10' },
      }),
    });
    dedup = new DedupService(config);
    dedup.onModuleInit();
    service = new Mt5ExecutionService(config, dedup);
    const record = service.createExecutionRecords([idea()])[0];
    signalId = record.signalId;
    dedup.markSeen([idea()], [record]);
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('checks readiness, authenticates, and persists a successful response', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready' }))
      .mockResolvedValueOnce(
        jsonResponse({ signal_id: signalId, outcome: 'filled' }),
      );
    service.setFetchImplementation(fetchMock);

    const summary = await service.processOutbox();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][0]).toBe(
      'http://127.0.0.1:8000/health/ready',
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      'http://127.0.0.1:8000/v1/signals',
    );
    const post = fetchMock.mock.calls[1][1] as RequestInit;
    expect(post.headers).toMatchObject({
      'Content-Type': 'application/json',
      'X-API-Key': 'test-api-key-value',
    });
    expect(JSON.parse(String(post.body))).not.toHaveProperty('entry_price');
    expect(dedup.listExecutions()[0]).toMatchObject({
      status: 'succeeded',
      attempts: 1,
    });
    expect(summary.submitted).toBe(1);
    expect(summary.succeeded).toBe(1);
  });

  it('logs the secret-safe submission lifecycle and broker execution details', async () => {
    const logSpy = jest.spyOn(Logger.prototype, 'log').mockImplementation();
    const warnSpy = jest.spyOn(Logger.prototype, 'warn').mockImplementation();
    service.setFetchImplementation(
      jest
        .fn()
        .mockResolvedValueOnce(jsonResponse({ status: 'ready' }))
        .mockResolvedValueOnce(
          jsonResponse({
            signal_id: signalId,
            outcome: 'filled',
            order_ticket: 1001,
            deal_ticket: 2002,
            execution_price: '1.09125',
            broker_retcode: 10009,
            metadata: { api_key: 'response-secret' },
          }),
        ),
    );

    let output = '';
    try {
      await service.processOutbox();
      output = [...logSpy.mock.calls, ...warnSpy.mock.calls]
        .flat()
        .map(String)
        .join('\n');
    } finally {
      logSpy.mockRestore();
      warnSpy.mockRestore();
    }

    expect(output).toContain('"event":"readiness_check_started"');
    expect(output).toContain('"event":"signal_submission_started"');
    expect(output).toContain(`"signalId":"${signalId}"`);
    expect(output).toContain('"symbol":"EURUSD"');
    expect(output).toContain('"direction":"buy"');
    expect(output).toContain('"volume":"0.10"');
    expect(output).toContain('"event":"signal_submission_response"');
    expect(output).toContain('"order_ticket":1001');
    expect(output).toContain('"deal_ticket":2002');
    expect(output).toContain('"event":"signal_state_changed"');
    expect(output).toContain('"status":"succeeded"');
    expect(output).toContain('"api_key":"[REDACTED]"');
    expect(output).not.toContain('test-api-key-value');
    expect(output).not.toContain('response-secret');
  });

  it('leaves requests pending and never POSTs when readiness fails', async () => {
    const fetchMock = jest.fn().mockResolvedValue(
      jsonResponse({ status: 'not_ready', details: {} }, 503),
    );
    service.setFetchImplementation(fetchMock);

    const summary = await service.processOutbox();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(dedup.listExecutions()[0]).toMatchObject({
      status: 'pending',
      attempts: 0,
    });
    expect(summary.pending).toBe(1);
  });

  it('reconciles a transport failure with GET 404 before retrying the same payload', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready' }))
      .mockRejectedValueOnce(new Error('connection reset'))
      .mockResolvedValueOnce(
        jsonResponse(
          { error: { code: 'signal_not_found', message: 'missing' } },
          404,
        ),
      )
      .mockResolvedValueOnce(jsonResponse({ status: 'ready' }))
      .mockResolvedValueOnce(
        jsonResponse({ signal_id: signalId, outcome: 'placed' }),
      );
    service.setFetchImplementation(fetchMock);

    await service.processOutbox();
    const afterFailure = dedup.listExecutions()[0];
    expect(afterFailure.status).toBe('pending');
    expect(afterFailure.attempts).toBe(1);

    await service.processOutbox();
    const afterRetry = dedup.listExecutions()[0];
    expect(afterRetry.status).toBe('succeeded');
    expect(afterRetry.attempts).toBe(2);
    const firstPost = fetchMock.mock.calls[1][1] as RequestInit;
    const secondPost = fetchMock.mock.calls[4][1] as RequestInit;
    expect(secondPost.body).toBe(firstPost.body);
    expect(fetchMock.mock.calls[2][0]).toBe(
      `http://127.0.0.1:8000/v1/signals/${signalId}`,
    );
  });

  it('aborts a timed-out POST and reconciles it before any retry', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready' }))
      .mockImplementationOnce(
        (_input: string | URL | Request, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener('abort', () => {
              const error = new Error('aborted');
              error.name = 'AbortError';
              reject(error);
            });
          }),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          { error: { code: 'signal_not_found', message: 'missing' } },
          404,
        ),
      );
    service.setFetchImplementation(fetchMock);

    await service.processOutbox();

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(dedup.listExecutions()[0]).toMatchObject({
      status: 'pending',
      attempts: 1,
    });
    expect(fetchMock.mock.calls[2][0]).toBe(
      `http://127.0.0.1:8000/v1/signals/${signalId}`,
    );
  });

  it.each([
    ['rejected', 'rejected'],
    ['unknown', 'unknown'],
    ['received', 'submitting'],
    ['executing', 'submitting'],
    ['partially_filled', 'succeeded'],
  ] as const)('maps MT5 status %s to outbox status %s', async (state, expected) => {
    dedup.updateExecution(signalId, { status: 'submitting' });
    const fetchMock = jest.fn().mockResolvedValue(
      jsonResponse({
        signal_id: signalId,
        state,
        response:
          state === 'partially_filled'
            ? { signal_id: signalId, outcome: 'partially_filled' }
            : null,
        error:
          state === 'rejected' || state === 'unknown'
            ? { code: `broker_${state}`, message: state }
            : null,
      }),
    );
    service.setFetchImplementation(fetchMock);

    await service.processOutbox();

    expect(dedup.listExecutions()[0].status).toBe(expected);
    expect(
      fetchMock.mock.calls.some((call) =>
        String(call[0]).endsWith('/health/ready'),
      ),
    ).toBe(false);
  });

  it('blocks authentication failures without exposing or retrying the key', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready' }))
      .mockResolvedValueOnce(
        jsonResponse(
          { error: { code: 'unauthorized', message: 'invalid key' } },
          401,
        ),
      );
    service.setFetchImplementation(fetchMock);

    await service.processOutbox();
    await service.processOutbox();

    expect(dedup.listExecutions()[0]).toMatchObject({
      status: 'blocked',
      error: { code: 'unauthorized' },
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(JSON.stringify(dedup.listExecutions()[0])).not.toContain(
      'test-api-key-value',
    );
  });

  it('treats an idempotency conflict as unknown', async () => {
    service.setFetchImplementation(
      jest
        .fn()
        .mockResolvedValueOnce(jsonResponse({ status: 'ready' }))
        .mockResolvedValueOnce(
          jsonResponse(
            {
              error: {
                code: 'idempotency_conflict',
                message: 'payload mismatch',
              },
            },
            409,
          ),
        ),
    );

    await service.processOutbox();

    expect(dedup.listExecutions()[0].status).toBe('unknown');
  });

  it('uses GET state to persist a structured broker rejection', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready' }))
      .mockResolvedValueOnce(
        jsonResponse(
          { error: { code: 'preflight_rejected', message: 'No money' } },
          422,
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          signal_id: signalId,
          state: 'rejected',
          response: null,
          error: { code: 'preflight_rejected', message: 'No money' },
        }),
      );
    service.setFetchImplementation(fetchMock);

    await service.processOutbox();

    expect(dedup.listExecutions()[0]).toMatchObject({
      status: 'rejected',
      error: { code: 'preflight_rejected' },
    });
  });
});
