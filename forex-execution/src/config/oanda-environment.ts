import { ApplicationError } from '../common/errors/application-error.js';

export interface OandaEnvironment {
  restBaseUrl: string;
  streamBaseUrl: string;
  isLive: boolean;
}

export function resolveOandaEnvironment(environment: string): OandaEnvironment {
  if (environment === 'practice') {
    return {
      restBaseUrl: 'https://api-fxpractice.oanda.com',
      streamBaseUrl: 'https://stream-fxpractice.oanda.com',
      isLive: false,
    };
  }
  if (environment === 'live') {
    return {
      restBaseUrl: 'https://api-fxtrade.oanda.com',
      streamBaseUrl: 'https://stream-fxtrade.oanda.com',
      isLive: true,
    };
  }
  throw new ApplicationError('INVALID_OANDA_ENVIRONMENT', 'Unsupported OANDA environment.', 500);
}
