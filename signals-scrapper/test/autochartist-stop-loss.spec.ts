import {
  autochartistMarketEntry,
  autochartistStopLoss,
} from '../src/autochartist/autochartist-stop-loss';
import {
  refreshAutochartistRequestStopLoss,
} from '../src/mt5/mt5-signal.mapper';
import { Mt5SignalRequest } from '../src/mt5/mt5.types';

describe('autochartistStopLoss', () => {
  it('mirrors the forecast across entry for UP trades', () => {
    expect(autochartistStopLoss(16.5, 16.6)).toBeCloseTo(16.4, 6);
  });

  it('mirrors the forecast across entry for DOWN trades', () => {
    expect(autochartistStopLoss(1.09, 1.08)).toBeCloseTo(1.1, 6);
  });
});

describe('autochartistMarketEntry', () => {
  it('uses ask for buys and bid for sells', () => {
    expect(autochartistMarketEntry('UP', 16.58, 16.59)).toBe(16.59);
    expect(autochartistMarketEntry('buy', 16.58, 16.59)).toBe(16.59);
    expect(autochartistMarketEntry('DOWN', 16.58, 16.59)).toBe(16.58);
    expect(autochartistMarketEntry('sell', 16.58, 16.59)).toBe(16.58);
  });
});

describe('refreshAutochartistRequestStopLoss', () => {
  const request: Mt5SignalRequest = {
    signal_id: '00000000-0000-5000-8000-000000000001',
    occurred_at: '2026-07-21T15:00:00.000Z',
    execution_type: 'market',
    symbol: 'USDZAR',
    direction: 'buy',
    volume: '0.05',
    stop_loss: '16.4',
    take_profit: '16.6',
    note: 'hash',
    source: 'autochartist',
  };

  it('recomputes stop loss from live bid/ask and the stored forecast', () => {
    const refreshed = refreshAutochartistRequestStopLoss(request, 16.58, 16.59);
    expect(refreshed.stop_loss).toBe(String(2 * 16.59 - 16.6));
    expect(refreshed.take_profit).toBe('16.6');
  });
});
