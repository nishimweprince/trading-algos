import { Injectable, Logger } from '@nestjs/common';
import { z } from 'zod';
import { AppConfigService } from '../config/app-config.service';
import { DedupService } from '../dedup/dedup.service';
import { TradingIdea } from '../models/trading-idea.model';
import {
  Mt5SignalMapper,
  refreshAutochartistRequestStopLoss,
} from './mt5-signal.mapper';
import {
  Mt5DeliverySummary,
  Mt5ExecutionError,
  Mt5ExecutionRecord,
  Mt5ExecutionStatus,
  Mt5SignalRequest,
} from './mt5.types';

const SuccessOutcomeSchema = z.enum([
  'filled',
  'partially_filled',
  'placed',
]);

const SuccessResponseSchema = z
  .object({
    signal_id: z.string().uuid(),
    outcome: SuccessOutcomeSchema,
  })
  .passthrough();

const StatusResponseSchema = z
  .object({
    signal_id: z.string().uuid(),
    state: z.enum([
      'received',
      'executing',
      'filled',
      'partially_filled',
      'placed',
      'rejected',
      'unknown',
    ]),
    response: z.unknown().optional().nullable(),
    error: z.unknown().optional().nullable(),
  })
  .passthrough();

const ErrorResponseSchema = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
    details: z.unknown().optional().nullable(),
  }),
});

const TickResponseSchema = z.object({
  symbol: z.string(),
  bid: z.number().finite(),
  ask: z.number().finite(),
});

type FetchImplementation = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

const LOG_SECRET_FIELD =
  /(?:api[-_]?key|authorization|password|secret|token|credential)/i;

function redactForLog(value: unknown, seen = new WeakSet<object>()): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => redactForLog(item, seen));
  }
  if (typeof value !== 'object' || value === null) return value;
  if (seen.has(value)) return '[Circular]';
  seen.add(value);
  const redacted: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    redacted[key] = LOG_SECRET_FIELD.test(key)
      ? '[REDACTED]'
      : redactForLog(item, seen);
  }
  return redacted;
}

@Injectable()
export class Mt5ExecutionService {
  private readonly logger = new Logger(Mt5ExecutionService.name);
  private readonly mapper: Mt5SignalMapper;
  private fetchImplementation: FetchImplementation = fetch;

  constructor(
    private readonly config: AppConfigService,
    private readonly dedup: DedupService,
  ) {
    this.mapper = new Mt5SignalMapper(config);
  }

  createExecutionRecords(ideas: TradingIdea[]): Mt5ExecutionRecord[] {
    const records = this.mapper.createRecords(ideas);
    for (const record of records) {
      if (record.status === 'skipped') {
        this.logEvent(
          'signal_skipped',
          {
            signalId: record.signalId,
            ideaHash: record.ideaHash,
            reason: record.error?.code,
            message: record.error?.message,
          },
          'warn',
        );
      }
    }
    return records;
  }

  /** Test-only HTTP injection; production uses the Node fetch implementation. */
  setFetchImplementation(fetchImplementation: FetchImplementation): void {
    this.fetchImplementation = fetchImplementation;
  }

  async processOutbox(): Promise<Mt5DeliverySummary> {
    const summary = this.emptySummary();
    if (!this.config.mt5SignalTradingEnabled) return summary;

    const submitting = this.dedup.listExecutions('submitting');
    const initiallyPending = this.dedup.listExecutions('pending');
    if (submitting.length > 0 || initiallyPending.length > 0) {
      this.logEvent('outbox_processing_started', {
        submitting: submitting.length,
        pending: initiallyPending.length,
      });
    }

    for (const record of submitting) {
      summary.reconciled += 1;
      await this.reconcile(record, 'pending');
    }

    const pending = this.dedup.listExecutions('pending');
    if (pending.length > 0) {
      const ready = await this.isReady(pending.length);
      if (ready) {
        for (const record of pending) {
          summary.submitted += 1;
          await this.submit(record);
        }
      } else {
        this.logger.warn(
          `MT5 is not ready; leaving ${pending.length} execution(s) pending`,
        );
      }
    }

    const current = this.dedup.listExecutions();
    summary.succeeded = current.filter(
      (record) => record.status === 'succeeded',
    ).length;
    summary.rejected = current.filter(
      (record) => record.status === 'rejected',
    ).length;
    summary.unknown = current.filter(
      (record) => record.status === 'unknown',
    ).length;
    summary.blocked = current.filter(
      (record) => record.status === 'blocked',
    ).length;
    summary.pending = current.filter(
      (record) =>
        record.status === 'pending' || record.status === 'submitting',
    ).length;
    if (summary.submitted > 0 || summary.reconciled > 0) {
      this.logger.log(
        `MT5 outbox: submitted=${summary.submitted}, reconciled=${summary.reconciled}, succeeded=${summary.succeeded}, rejected=${summary.rejected}, unknown=${summary.unknown}, blocked=${summary.blocked}, pending=${summary.pending}`,
      );
    }
    return summary;
  }

  private async isReady(pendingCount: number): Promise<boolean> {
    const startedAt = Date.now();
    this.logEvent('readiness_check_started', {
      endpoint: this.endpoint('/health/ready'),
      pending: pendingCount,
    });
    try {
      const response = await this.request('/health/ready');
      const body = await this.readJson(response);
      const ready =
        response.ok &&
        typeof body === 'object' &&
        body !== null &&
        (body as { status?: unknown }).status === 'ready';
      this.logEvent(
        'readiness_check_completed',
        {
          ready,
          httpStatus: response.status,
          elapsedMs: Date.now() - startedAt,
          reportedStatus:
            typeof body === 'object' && body !== null
              ? (body as { status?: unknown }).status
              : undefined,
        },
        ready ? 'log' : 'warn',
      );
      return ready;
    } catch (err) {
      this.logEvent(
        'readiness_check_failed',
        {
          elapsedMs: Date.now() - startedAt,
          error: this.safeMessage(err),
        },
        'warn',
      );
      return false;
    }
  }

  private async submit(record: Mt5ExecutionRecord): Promise<void> {
    if (!record.request) {
      this.transition(record.signalId, 'blocked', {
        code: 'missing_request',
        message: 'The durable execution record has no request payload',
      });
      return;
    }

    const startedAt = new Date().toISOString();
    const submitting = this.dedup.updateExecution(record.signalId, {
      status: 'submitting',
      attempts: record.attempts + 1,
      lastAttemptAt: startedAt,
      updatedAt: startedAt,
      error: undefined,
    });
    const request = await this.resolveSubmitRequest(submitting);
    const requestStartedAt = Date.now();
    this.logEvent('signal_submission_started', {
      signalId: submitting.signalId,
      attempt: submitting.attempts,
      endpoint: this.endpoint('/v1/signals'),
      request,
    });

    let response: Response;
    try {
      response = await this.request('/v1/signals', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-API-Key': this.config.mt5SignalApiKey,
        },
        body: JSON.stringify(request),
      });
    } catch (err) {
      this.dedup.updateExecution(record.signalId, {
        error: {
          code: 'transport_error',
          message: this.safeMessage(err),
        },
      });
      this.logEvent(
        'signal_submission_transport_error',
        {
          signalId: submitting.signalId,
          attempt: submitting.attempts,
          elapsedMs: Date.now() - requestStartedAt,
          error: this.safeMessage(err),
          nextAction: 'reconcile_before_retry',
        },
        'warn',
      );
      await this.reconcile(submitting, 'pending');
      return;
    }

    const body = await this.readJson(response);
    this.logEvent(
      'signal_submission_response',
      {
        signalId: submitting.signalId,
        attempt: submitting.attempts,
        httpStatus: response.status,
        elapsedMs: Date.now() - requestStartedAt,
        response: body,
      },
      response.ok ? 'log' : 'warn',
    );
    if (response.ok) {
      const parsed = SuccessResponseSchema.safeParse(body);
      if (parsed.success) {
        this.transition(record.signalId, 'succeeded', undefined, parsed.data);
        return;
      }
      await this.reconcile(submitting, 'submitting');
      return;
    }

    const apiError = this.parseApiError(body, response.status);
    if (response.status === 401) {
      this.transition(record.signalId, 'blocked', apiError);
      return;
    }
    if (apiError.code === 'idempotency_conflict') {
      this.transition(record.signalId, 'unknown', apiError);
      return;
    }
    if (apiError.code === 'execution_outcome_unknown') {
      this.transition(record.signalId, 'unknown', apiError);
      return;
    }

    this.dedup.updateExecution(record.signalId, { error: apiError });
    await this.reconcile(
      submitting,
      response.status === 422 ? 'rejected' : 'pending',
      apiError,
    );
  }

  private async reconcile(
    record: Mt5ExecutionRecord,
    missingStatus: 'pending' | 'rejected' | 'submitting',
    fallbackError?: Mt5ExecutionError,
  ): Promise<void> {
    const startedAt = Date.now();
    this.logEvent('signal_reconciliation_started', {
      signalId: record.signalId,
      currentStatus: record.status,
      attempt: record.attempts,
      reason: fallbackError?.code,
    });
    let response: Response;
    try {
      response = await this.request(`/v1/signals/${record.signalId}`, {
        headers: { 'X-API-Key': this.config.mt5SignalApiKey },
      });
    } catch (err) {
      this.dedup.updateExecution(record.signalId, {
        status: 'submitting',
        error: fallbackError ?? {
          code: 'status_unavailable',
          message: this.safeMessage(err),
        },
      });
      this.logEvent(
        'signal_reconciliation_failed',
        {
          signalId: record.signalId,
          elapsedMs: Date.now() - startedAt,
          error: this.safeMessage(err),
          state: 'submitting',
          operatorAction: 'retry_reconciliation_later',
        },
        'warn',
      );
      return;
    }

    if (response.status === 404) {
      this.logEvent('signal_reconciliation_not_found', {
        signalId: record.signalId,
        httpStatus: response.status,
        elapsedMs: Date.now() - startedAt,
        nextState: missingStatus,
      });
      this.transition(record.signalId, missingStatus, fallbackError);
      return;
    }

    const body = await this.readJson(response);
    this.logEvent(
      'signal_reconciliation_response',
      {
        signalId: record.signalId,
        httpStatus: response.status,
        elapsedMs: Date.now() - startedAt,
        response: body,
      },
      response.ok ? 'log' : 'warn',
    );
    if (response.status === 401) {
      this.transition(
        record.signalId,
        'blocked',
        this.parseApiError(body, response.status),
      );
      return;
    }
    if (!response.ok) {
      this.dedup.updateExecution(record.signalId, {
        status: 'submitting',
        error: this.parseApiError(body, response.status),
      });
      return;
    }

    const parsed = StatusResponseSchema.safeParse(body);
    if (!parsed.success) {
      this.dedup.updateExecution(record.signalId, {
        status: 'submitting',
        error: {
          code: 'invalid_status_response',
          message: 'MT5 returned an invalid signal status response',
        },
      });
      return;
    }

    const status = parsed.data;
    if (SuccessOutcomeSchema.safeParse(status.state).success) {
      this.transition(
        record.signalId,
        'succeeded',
        undefined,
        status.response ?? status,
      );
    } else if (status.state === 'rejected') {
      this.transition(record.signalId, 'rejected', this.statusError(status.error));
    } else if (status.state === 'unknown') {
      this.transition(record.signalId, 'unknown', this.statusError(status.error));
    } else {
      this.transition(record.signalId, 'submitting', fallbackError);
    }
  }

  private transition(
    signalId: string,
    status: Mt5ExecutionStatus,
    error?: Mt5ExecutionError,
    response?: unknown,
  ): void {
    const updated = this.dedup.updateExecution(signalId, {
      status,
      error,
      response,
      updatedAt: new Date().toISOString(),
    });
    const level =
      status === 'rejected' || status === 'unknown' || status === 'blocked'
        ? 'warn'
        : 'log';
    this.logEvent(
      'signal_state_changed',
      {
        signalId,
        status,
        attempt: updated.attempts,
        symbol: updated.request?.symbol,
        direction: updated.request?.direction,
        error,
        response,
      },
      level,
    );
  }

  private async resolveSubmitRequest(
    record: Mt5ExecutionRecord,
  ): Promise<Mt5SignalRequest> {
    if (!record.request) {
      throw new Error('Execution record is missing a request payload');
    }
    if (record.request.source !== 'autochartist') {
      return record.request;
    }
    return this.refreshAutochartistStopLoss(record.request);
  }

  private async refreshAutochartistStopLoss(
    request: Mt5SignalRequest,
  ): Promise<Mt5SignalRequest> {
    try {
      const response = await this.request(
        `/v1/market-data/tick?quote=${encodeURIComponent(request.symbol)}`,
        { headers: { 'X-API-Key': this.config.mt5SignalApiKey } },
      );
      const body = await this.readJson(response);
      if (!response.ok) {
        this.logEvent(
          'autochartist_tick_unavailable',
          {
            symbol: request.symbol,
            httpStatus: response.status,
            response: body,
          },
          'warn',
        );
        return request;
      }

      const parsed = TickResponseSchema.safeParse(body);
      if (!parsed.success) {
        this.logEvent(
          'autochartist_tick_invalid',
          { symbol: request.symbol, response: body },
          'warn',
        );
        return request;
      }

      const refreshed = refreshAutochartistRequestStopLoss(
        request,
        parsed.data.bid,
        parsed.data.ask,
      );
      if (refreshed.stop_loss !== request.stop_loss) {
        this.logEvent('autochartist_stop_loss_refreshed', {
          symbol: request.symbol,
          direction: request.direction,
          forecast: request.take_profit,
          previousStopLoss: request.stop_loss,
          stopLoss: refreshed.stop_loss,
          bid: parsed.data.bid,
          ask: parsed.data.ask,
        });
      }
      return refreshed;
    } catch (err) {
      this.logEvent(
        'autochartist_tick_fetch_failed',
        { symbol: request.symbol, error: this.safeMessage(err) },
        'warn',
      );
      return request;
    }
  }

  private async request(path: string, init: RequestInit = {}): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(
      () => controller.abort(),
      this.config.mt5SignalTimeoutMs,
    );
    try {
      return await this.fetchImplementation(
        this.endpoint(path),
        { ...init, signal: controller.signal },
      );
    } finally {
      clearTimeout(timer);
    }
  }

  private async readJson(response: Response): Promise<unknown> {
    const text = await response.text();
    if (!text.trim()) return undefined;
    try {
      return JSON.parse(text) as unknown;
    } catch {
      return undefined;
    }
  }

  private parseApiError(body: unknown, status: number): Mt5ExecutionError {
    const parsed = ErrorResponseSchema.safeParse(body);
    if (parsed.success) {
      return {
        code: parsed.data.error.code,
        message: parsed.data.error.message,
        details: parsed.data.error.details ?? undefined,
      };
    }
    return {
      code: `http_${status}`,
      message: `MT5 returned HTTP ${status}`,
    };
  }

  private statusError(error: unknown): Mt5ExecutionError {
    if (typeof error === 'object' && error !== null) {
      const value = error as Record<string, unknown>;
      if (typeof value.code === 'string' && typeof value.message === 'string') {
        return {
          code: value.code,
          message: value.message,
          details: value.details,
        };
      }
    }
    return {
      code: 'mt5_terminal_state',
      message: 'MT5 returned a terminal state without structured error details',
    };
  }

  private safeMessage(error: unknown): string {
    if (error instanceof Error && error.name === 'AbortError') {
      return 'MT5 request timed out';
    }
    return error instanceof Error ? error.message : String(error);
  }

  private endpoint(path: string): string {
    return `${this.config.mt5SignalApiUrl.replace(/\/$/, '')}${path}`;
  }

  private logEvent(
    event: string,
    details: Record<string, unknown>,
    level: 'log' | 'warn' = 'log',
  ): void {
    const message = `MT5 execution ${JSON.stringify(
      redactForLog({ event, ...details }),
    )}`;
    this.logger[level](message);
  }

  private emptySummary(): Mt5DeliverySummary {
    return {
      reconciled: 0,
      submitted: 0,
      succeeded: 0,
      rejected: 0,
      unknown: 0,
      blocked: 0,
      pending: 0,
    };
  }
}
