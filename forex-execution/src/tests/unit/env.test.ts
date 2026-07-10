import { describe, expect, it } from 'vitest';
import { loadConfig } from '../../config/env.js';

const baseEnv = {
  INTERNAL_API_KEY: 'replace-with-a-long-random-value',
  OANDA_ACCOUNT_ID: '101-001-123456-001',
  OANDA_API_TOKEN: 'secret-token',
  DATABASE_URL: 'postgresql://postgres:postgres@localhost:5432/oanda_trading',
  REDIS_URL: 'redis://localhost:6379',
};

describe('loadConfig', () => {
  it('defaults to practice and disables live trading', () => {
    const config = loadConfig(baseEnv);
    expect(config.OANDA_ENV).toBe('practice');
    expect(config.LIVE_TRADING_ENABLED).toBe(false);
  });

  it('refuses live mode without explicit live switch', () => {
    expect(() => loadConfig({ ...baseEnv, OANDA_ENV: 'live', LIVE_TRADING_ENABLED: 'false' })).toThrow('Environment configuration is invalid.');
  });

  it('accepts live mode only with explicit live switch', () => {
    const config = loadConfig({ ...baseEnv, OANDA_ENV: 'live', LIVE_TRADING_ENABLED: 'true' });
    expect(config.oandaEnvironment.isLive).toBe(true);
  });
});
