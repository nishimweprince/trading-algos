import axios from 'axios';
import { BrokerError } from '../common/errors/broker-error.js';

type OandaErrorPayload = { errorCode?: string; errorMessage?: string; message?: string };

function isOandaErrorPayload(value: unknown): value is OandaErrorPayload {
  return typeof value === 'object' && value !== null;
}

export function mapOandaError(error: unknown): BrokerError {
  if (axios.isAxiosError<unknown>(error)) {
    const status = error.response?.status ?? 503;
    const requestId = getHeader(error.response?.headers, 'requestid') ?? getHeader(error.response?.headers, 'request-id');
    const payload: unknown = error.response?.data;
    const brokerCode = isOandaErrorPayload(payload) ? payload.errorCode : undefined;
    const brokerMessage = isOandaErrorPayload(payload) ? payload.errorMessage ?? payload.message : undefined;
    const code = status === 401 ? 'AUTHENTICATION_FAILED' : status === 429 ? 'BROKER_RATE_LIMITED' : status >= 500 ? 'BROKER_TIMEOUT' : 'BROKER_REJECTED';
    return new BrokerError(code, brokerMessage ?? error.message, status, typeof requestId === 'string' ? requestId : undefined, brokerCode ? { brokerCode } : undefined);
  }
  return new BrokerError('BROKER_TIMEOUT', 'Unable to communicate with OANDA.', 503);
}

function getHeader(headers: unknown, name: string): string | undefined {
  if (typeof headers !== 'object' || headers === null) return undefined;
  const value = (headers as Record<string, unknown>)[name];
  if (typeof value === 'string') return value;
  if (Array.isArray(value) && typeof value[0] === 'string') return value[0];
  return undefined;
}
