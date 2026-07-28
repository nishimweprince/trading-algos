import { AppConfigService } from '../src/config/app-config.service';
import { TradingIdea } from '../src/models/trading-idea.model';
import {
  deterministicSignalId,
  Mt5SignalMapper,
} from '../src/mt5/mt5-signal.mapper';

function config(enabled = true): AppConfigService {
  const value = new AppConfigService();
  value.load({
    SOURCES: JSON.stringify([
      { type: 'TRADING_CENTRAL', url: 'https://example.com/tc' },
    ]),
    MT5_SIGNAL_TRADING_ENABLED: String(enabled),
    MT5_SIGNAL_API_KEY: enabled ? 'test-api-key-value' : '',
    MT5_SIGNAL_RULES: JSON.stringify({
      'EUR/USD': { symbol: 'EURUSD.a', volume: '0.10' },
    }),
  });
  return value;
}

function idea(overrides: Partial<TradingIdea> = {}): TradingIdea {
  return {
    provider: 'TRADING_CENTRAL',
    instrument: 'EUR/USD',
    timeframe: '30 MIN',
    direction: 'UP',
    entry: 1.09,
    stopLoss: 1.08,
    takeProfit: 1.1,
    ideaTimestamp: '2026-07-14T14:58:00.000Z',
    capturedAt: '2026-07-14T15:00:00.000Z',
    sourceUrl: 'https://example.com/tc',
    hash: 'a'.repeat(64),
    ...overrides,
  };
}

describe('MT5 signal mapper', () => {
  it('creates a stable market request with exact broker rule values', () => {
    const mapper = new Mt5SignalMapper(config());
    const first = mapper.createRecords([idea()])[0];
    const second = mapper.createRecords([idea({ entry: 1.091 })])[0];

    expect(first.signalId).toBe(second.signalId);
    expect(first.signalId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
    expect(first.request).toEqual({
      signal_id: first.signalId,
      occurred_at: '2026-07-14T15:00:00.000Z',
      execution_type: 'market',
      symbol: 'EURUSD.a',
      direction: 'buy',
      volume: '0.10',
      stop_loss: '1.08',
      take_profit: '1.1',
      note: 'a'.repeat(64),
      source: 'trading_central',
    });
    expect(first.request).not.toHaveProperty('entry_price');
  });

  it('maps DOWN to sell and compatibility pivot/target to risk fields', () => {
    const record = new Mt5SignalMapper(config()).createRecords([
      idea({
        direction: 'DOWN',
        stopLoss: undefined,
        takeProfit: undefined,
        pivot: 1.1,
        target: 1.08,
      }),
    ])[0];
    expect(record.request).toMatchObject({
      direction: 'sell',
      stop_loss: '1.1',
      take_profit: '1.08',
    });
  });

  it('does not queue while disabled or for neutral ideas', () => {
    expect(new Mt5SignalMapper(config(false)).createRecords([idea()])).toEqual([]);
    expect(
      new Mt5SignalMapper(config()).createRecords([
        idea({ direction: 'NEUTRAL' }),
      ]),
    ).toEqual([]);
  });

  it('maps an Autochartist idea with forecast-derived stop loss to a market request', () => {
    const record = new Mt5SignalMapper(config()).createRecords([
      idea({
        provider: 'AUTOCHARTIST',
        direction: 'UP',
        entry: 1.09,
        stopLoss: 1.07,
        takeProfit: 1.1,
        pivot: 1.07,
        target: 1.1,
        hash: '2'.repeat(64),
      }),
    ])[0];

    expect(record.status).toBe('pending');
    expect(record.request).toMatchObject({
      execution_type: 'market',
      symbol: 'EURUSD.a',
      direction: 'buy',
      stop_loss: '1.08',
      take_profit: '1.1',
      source: 'autochartist',
      occurred_at: '2026-07-14T15:00:00.000Z',
    });
  });

  it('audits unmapped instruments and missing risk levels as skipped', () => {
    const records = new Mt5SignalMapper(config()).createRecords([
      idea({ instrument: 'GBP/USD', hash: 'b'.repeat(64) }),
      idea({ stopLoss: undefined, takeProfit: undefined, hash: 'c'.repeat(64) }),
    ]);
    expect(records.map((record) => record.status)).toEqual(['skipped', 'skipped']);
    expect(records.map((record) => record.error?.code)).toEqual([
      'unmapped_instrument',
      'missing_risk_levels',
    ]);
    expect(records.every((record) => record.request === undefined)).toBe(true);
  });

  it('skips directionally invalid exit levels before submission', () => {
    const records = new Mt5SignalMapper(config()).createRecords([
      idea({
        direction: 'UP',
        stopLoss: 1.1,
        takeProfit: 1.08,
        hash: 'd'.repeat(64),
      }),
      idea({
        direction: 'DOWN',
        stopLoss: 0.8522,
        takeProfit: 0.8555,
        hash: 'e'.repeat(64),
      }),
    ]);

    expect(records.map((record) => record.status)).toEqual(['skipped', 'skipped']);
    expect(records.map((record) => record.error?.code)).toEqual([
      'invalid_risk_relationship',
      'invalid_risk_relationship',
    ]);
    expect(records.every((record) => record.request === undefined)).toBe(true);
  });

  it('skips exit levels that are an implausible magnitude relative to entry', () => {
    const record = new Mt5SignalMapper(config()).createRecords([
      idea({
        direction: 'UP',
        entry: 148.5,
        stopLoss: 1.4,
        takeProfit: 150,
        hash: 'f'.repeat(64),
      }),
    ])[0];

    expect(record.status).toBe('skipped');
    expect(record.error?.code).toBe('implausible_exit_level_magnitude');
    expect(record.request).toBeUndefined();
  });

  it('does not flag exit levels within a plausible distance of entry', () => {
    const record = new Mt5SignalMapper(config()).createRecords([
      idea({ direction: 'UP', entry: 1.09, stopLoss: 1.08, takeProfit: 1.1 }),
    ])[0];

    expect(record.status).toBe('pending');
    expect(record.request).toBeDefined();
  });

  it('skips the magnitude check entirely when entry is missing', () => {
    const record = new Mt5SignalMapper(config()).createRecords([
      idea({
        direction: 'UP',
        entry: undefined,
        stopLoss: 1.4,
        takeProfit: 150,
        hash: '1'.repeat(64),
      }),
    ])[0];

    expect(record.status).toBe('pending');
    expect(record.request).toBeDefined();
  });

  it('uses UUIDv5 deterministically for a hash', () => {
    expect(deterministicSignalId('hash')).toBe(deterministicSignalId('hash'));
    expect(deterministicSignalId('hash')).not.toBe(
      deterministicSignalId('other-hash'),
    );
  });
});
