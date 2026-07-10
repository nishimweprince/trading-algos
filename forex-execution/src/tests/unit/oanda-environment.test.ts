import { describe, expect, it } from 'vitest';
import { resolveOandaEnvironment } from '../../config/oanda-environment.js';

describe('resolveOandaEnvironment', () => {
  it('resolves practice URLs by default policy', () => {
    expect(resolveOandaEnvironment('practice')).toEqual({
      restBaseUrl: 'https://api-fxpractice.oanda.com',
      streamBaseUrl: 'https://stream-fxpractice.oanda.com',
      isLive: false,
    });
  });

  it('resolves live URLs', () => {
    expect(resolveOandaEnvironment('live').isLive).toBe(true);
  });
});
