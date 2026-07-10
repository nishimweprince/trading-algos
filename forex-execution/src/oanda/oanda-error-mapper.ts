import { AxiosError } from 'axios';
import { BrokerError } from '../common/errors/broker-error.js';

type OandaErrorPayload = { errorCode?: string; errorMessage?: string; message?: string };

function isOandaErrorPayload(value: unknown): value is OandaErrorPayload {
  return typeof value === 'object' && value !== null;
}

export function mapOandaError(error: unknown): BrokerError {
  if (error instanceof AxiosError) {
    const status = error.response?.status ?? 503;
    const requestId = error.response?.headers['requestid'] ?? error.response?.headers['request-id'];
    const payload = error.response?.data;
    const brokerCode = isOandaErrorPayload(payload) ? payload.errorCode : undefined;
    const brokerMessage = isOandaErrorPayload(payload) ? payload.errorMessage ?? payload.message : undefined;
    const code = status === 401 ? 'AUTHENTICATION_FAILED' : status === 429 ? 'BROKER_RATE_LIMITED' : status >= 500 ? 'BROKER_TIMEOUT' : 'BROKER_REJECTED';
    return new BrokerError(code, brokerMessage ?? error.message, status, typeof requestId === 'string' ? requestId : undefined, brokerCode ? { brokerCode } : undefined);
  }
  return new BrokerError('BROKER_TIMEOUT', 'Unable to communicate with OANDA.', 503);
}
