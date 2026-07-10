import axios, { AxiosInstance, AxiosRequestConfig, Method } from 'axios';
import { AppConfig } from '../config/env.js';
import { AppLogger } from '../common/logging/logger.js';
import { waitForRetry } from '../common/utilities/retry.js';
import { mapOandaError } from './oanda-error-mapper.js';

export interface RequestOptions { safeToRetry?: boolean; timeoutMs?: number }
export interface OandaClient {
  get<T>(path: string, options?: RequestOptions): Promise<T>;
  post<T>(path: string, body: unknown, options?: RequestOptions): Promise<T>;
  put<T>(path: string, body: unknown, options?: RequestOptions): Promise<T>;
  patch<T>(path: string, body: unknown, options?: RequestOptions): Promise<T>;
  delete<T>(path: string, options?: RequestOptions): Promise<T>;
}

export class AxiosOandaClient implements OandaClient {
  private readonly http: AxiosInstance;
  constructor(private readonly config: AppConfig, private readonly logger: AppLogger) {
    this.http = axios.create({
      baseURL: config.oandaEnvironment.restBaseUrl,
      timeout: config.REST_REQUEST_TIMEOUT_MS,
      headers: {
        Authorization: `Bearer ${config.OANDA_API_TOKEN}`,
        'Content-Type': 'application/json',
        'User-Agent': `${config.APP_NAME}/1.0`,
        'OANDA-Agent': config.APP_NAME,
      },
    });
  }
  get<T>(path: string, options?: RequestOptions) { return this.request<T>('GET', path, undefined, { safeToRetry: true, ...options }); }
  post<T>(path: string, body: unknown, options?: RequestOptions) { return this.request<T>('POST', path, body, options); }
  put<T>(path: string, body: unknown, options?: RequestOptions) { return this.request<T>('PUT', path, body, options); }
  patch<T>(path: string, body: unknown, options?: RequestOptions) { return this.request<T>('PATCH', path, body, options); }
  delete<T>(path: string, options?: RequestOptions) { return this.request<T>('DELETE', path, undefined, options); }

  private async request<T>(method: Method, path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    const attempts = options?.safeToRetry ? this.config.MAX_RETRY_ATTEMPTS : 1;
    for (let attempt = 1; attempt <= attempts; attempt += 1) {
      const started = Date.now();
      try {
        const requestConfig: AxiosRequestConfig = { method, url: path, data: body };
        if (options?.timeoutMs !== undefined) requestConfig.timeout = options.timeoutMs;
        const response = await this.http.request<T>(requestConfig);
        this.logger.info({ method, path, status: response.status, oandaRequestId: response.headers.requestid, durationMs: Date.now() - started }, 'OANDA request completed');
        return response.data;
      } catch (error) {
        const mapped = mapOandaError(error);
        const retryable = options?.safeToRetry === true && ['BROKER_RATE_LIMITED', 'BROKER_TIMEOUT'].includes(mapped.code) && attempt < attempts;
        this.logger.warn({ method, path, code: mapped.code, statusCode: mapped.statusCode, attempt, retryable, durationMs: Date.now() - started }, 'OANDA request failed');
        if (!retryable) throw mapped;
        await waitForRetry(attempt, this.config.RETRY_BASE_DELAY_MS);
      }
    }
    throw mapOandaError(undefined);
  }
}
