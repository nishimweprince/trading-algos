/**
 * Autochartist cards expose a Forecast level but no stop. Mirror the forecast
 * across entry for a 1:1 risk-reward: stopLoss = 2 * entry - forecast.
 */
export function autochartistStopLoss(entry: number, forecast: number): number {
  return 2 * entry - forecast;
}

/** Market entry proxy: buy at ask, sell at bid. */
export function autochartistMarketEntry(
  direction: 'UP' | 'DOWN' | 'buy' | 'sell',
  bid: number,
  ask: number,
): number {
  const isBuy = direction === 'UP' || direction === 'buy';
  return isBuy ? ask : bid;
}
