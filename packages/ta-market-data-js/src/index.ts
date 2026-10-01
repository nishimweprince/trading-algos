/**
 * Client for market-data-service, the only place JavaScript consumers should
 * get prices from. It never talks to an exchange or broker itself.
 *
 * Mirrors the Python `ta_clients.MarketDataClient`: one instance per market
 * (`forex`, `deriv`, `crypto`), candles are closed bars stamped at the UTC END
 * of their interval, and large candle requests page backwards on `to`.
 * Runtime dependencies: none (global `fetch`, Node 18.17+).
 */

export type Market = 'forex' | 'deriv' | 'crypto';

export type Timeframe =
  | 'M1' | 'M2' | 'M3' | 'M4' | 'M5' | 'M10' | 'M15' | 'M30'
  | 'H1' | 'H4' | 'H12' | 'D1' | 'W1';

export const TIMEFRAME_MINUTES: Record<Timeframe, number> = {
  M1: 1, M2: 2, M3: 3, M4: 4, M5: 5, M10: 10, M15: 15, M30: 30,
  H1: 60, H4: 240, H12: 720, D1: 1440, W1: 10080,
};

export interface MarketQuote {
  symbol: string;
  source_instrument: string;
  provider: string;
  /** ISO-8601, UTC. */
  ts: string;
  /** Mid when both sides are quoted. */
  price: number;
  bid: number | null;
  ask: number | null;
  spread: number | null;
}

export interface Candle {
  /** ISO-8601, UTC, the END of the bar's interval. */
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  provider: string;
  source_instrument: string;
  spread: number | null;
  spread_source: string | null;
}

export interface InstrumentInfo {
  symbol: string;
  source_instrument: string;
  provider: string;
  digits: number;
  description: string | null;
  price_increment: number | null;
  quantity_increment: number | null;
  min_quantity: number | null;
  max_quantity: number | null;
}

export interface Capabilities {
  market: Market;
  provider: string;
  timeframes: Timeframe[];
  streaming: boolean;
  bid_ask: boolean;
  max_candles: number;
}

export interface StreamStatus {
  state: 'starting' | 'connected' | 'reconnecting' | 'stopped';
  dropped: number;
  error?: string | null;
}

export type StreamEvent =
  | { event: 'tick'; data: MarketQuote }
  | { event: 'status'; data: StreamStatus };

export interface MarketDataClientOptions {
  baseUrl: string;
  apiKey: string;
  market: Market;
  /** Ask for a specific provider; the service rejects one it is not configured with. */
  provider?: string;
  timeoutMs?: number;
  /** Bars per request when paging; the service caps it at MAX_CANDLES_LOOKBACK. */
  pageSize?: number;
  fetch?: typeof fetch;
}

/** A non-2xx answer, carrying the service's structured error. */
export class MarketDataError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = 'MarketDataError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export interface Subscription {
  close(): void;
  /** Settles when the stream ends: closed, or failed (rejects). */
  done: Promise<void>;
}

export class MarketDataClient {
  readonly market: Market;
  private readonly base: string;
  private readonly apiKey: string;
  private readonly provider?: string;
  private readonly timeoutMs: number;
  private readonly pageSize: number;
  private readonly fetchImpl: typeof fetch;

  constructor(options: MarketDataClientOptions) {
    this.base = options.baseUrl.replace(/\/+$/, '');
    this.apiKey = options.apiKey;
    this.market = options.market;
    this.provider = options.provider;
    this.timeoutMs = options.timeoutMs ?? 10_000;
    this.pageSize = options.pageSize ?? 1000;
    const impl = options.fetch ?? globalThis.fetch;
    if (!impl) throw new Error('global fetch is unavailable; use Node 18.17 or newer');
    this.fetchImpl = impl;
  }

  async quote(symbol: string): Promise<MarketQuote> {
    return this.get<MarketQuote>('tick', { symbol });
  }

  async instruments(): Promise<InstrumentInfo[]> {
    const body = await this.get<{ instruments: InstrumentInfo[] }>('symbols', {});
    return body.instruments;
  }

  async capabilities(): Promise<Capabilities> {
    return this.get<Capabilities>('capabilities', {});
  }

  /** Up to `count` closed bars ending at or before `to` (default now), oldest first. */
  async candles(
    symbol: string,
    timeframe: Timeframe,
    count: number,
    options: { to?: Date | string } = {},
  ): Promise<Candle[]> {
    const collected = new Map<string, Candle>();
    let cursor = options.to === undefined ? undefined : toIso(options.to);
    while (collected.size < count) {
      // `to` is inclusive, so a page after the first repeats the bar at the
      // cursor; ask for one more so the overlap does not cost a bar.
      const overlap = cursor === undefined ? 0 : 1;
      const take = Math.min(this.pageSize, count - collected.size + overlap);
      const params: Record<string, string> = { symbol, timeframe, count: String(take) };
      if (cursor !== undefined) params.to = cursor;
      const page = await this.get<{ candles: Candle[] }>('candles', params);
      const fresh = page.candles.filter((candle) => !collected.has(candle.ts));
      if (fresh.length === 0) break;
      for (const candle of fresh) collected.set(candle.ts, candle);
      const oldest = [...collected.keys()].sort(byTime)[0];
      if (cursor !== undefined && Date.parse(oldest) >= Date.parse(cursor)) break;
      cursor = oldest;
    }
    return [...collected.values()].sort((a, b) => byTime(a.ts, b.ts)).slice(-count);
  }

  /** Every closed bar whose interval ends in [from, to]. */
  async candlesRange(
    symbol: string,
    timeframe: Timeframe,
    from: Date | string,
    to?: Date | string,
  ): Promise<Candle[]> {
    const start = Date.parse(toIso(from));
    const end = to === undefined ? Date.now() : Date.parse(toIso(to));
    const minutes = TIMEFRAME_MINUTES[timeframe];
    const count = Math.floor(Math.max((end - start) / 60_000, minutes) / minutes) + 8;
    const candles = await this.candles(symbol, timeframe, count, to === undefined ? {} : { to });
    return candles.filter((candle) => {
      const ts = Date.parse(candle.ts);
      return ts >= start && ts <= end;
    });
  }

  /** The service's own readiness, which includes quote staleness. */
  async ready(): Promise<{ ready: boolean; reason: string }> {
    try {
      const response = await this.fetchImpl(`${this.base}/health/ready`, {
        signal: AbortSignal.timeout(5_000),
      });
      return response.ok
        ? { ready: true, reason: 'ok' }
        : { ready: false, reason: `status ${response.status}` };
    } catch (error) {
      return { ready: false, reason: (error as Error).message };
    }
  }

  /**
   * Live quotes over SSE. The service replays the newest quote per symbol on
   * connect, then pushes each change; `status` events report the upstream
   * connection and how many ticks this consumer dropped by being slow.
   */
  stream(
    symbols: string[] | undefined,
    onEvent: (event: StreamEvent) => void,
  ): Subscription {
    const controller = new AbortController();
    const params: Record<string, string> = {};
    if (symbols && symbols.length > 0) params.symbols = symbols.join(',');
    const url = this.url('stream/ticks', params);
    const done = (async () => {
      const response = await this.fetchImpl(url, {
        headers: { ...this.headers(), Accept: 'text/event-stream' },
        signal: controller.signal,
      });
      if (!response.ok || !response.body) throw await toError(response);
      const decoder = new TextDecoder();
      let buffer = '';
      const reader = response.body.getReader();
      try {
        for (;;) {
          const { value, done: finished } = await reader.read();
          if (finished) break;
          buffer += decoder.decode(value, { stream: true });
          let boundary: number;
          while ((boundary = buffer.search(/\r?\n\r?\n/)) !== -1) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary).replace(/^\r?\n\r?\n/, '');
            const parsed = parseSse(block);
            if (parsed) onEvent(parsed);
          }
        }
      } catch (error) {
        if (!controller.signal.aborted) throw error;
      }
    })().catch((error: unknown) => {
      if (controller.signal.aborted) return;
      throw error;
    });
    return { close: () => controller.abort(), done };
  }

  // --- transport -------------------------------------------------------------

  private headers(): Record<string, string> {
    return { 'X-API-Key': this.apiKey };
  }

  private url(route: string, params: Record<string, string>): string {
    const query = new URLSearchParams(params);
    if (this.provider) query.set('provider', this.provider);
    const qs = query.toString();
    return `${this.base}/v1/${this.market}/${route}${qs ? `?${qs}` : ''}`;
  }

  private async get<T>(route: string, params: Record<string, string>): Promise<T> {
    const response = await this.fetchImpl(this.url(route, params), {
      headers: this.headers(),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!response.ok) throw await toError(response);
    return (await response.json()) as T;
  }
}

function toIso(value: Date | string): string {
  return value instanceof Date ? value.toISOString() : new Date(value).toISOString();
}

function byTime(a: string, b: string): number {
  return Date.parse(a) - Date.parse(b);
}

async function toError(response: Response): Promise<MarketDataError> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }
  const error = (body as { error?: { code?: string; message?: string; details?: unknown } })
    ?.error;
  return new MarketDataError(
    response.status,
    error?.code ?? 'http_error',
    error?.message ?? `market-data-service answered ${response.status}`,
    error?.details,
  );
}

function parseSse(block: string): StreamEvent | undefined {
  let event = 'message';
  const data: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith(':')) continue; // keepalive comment
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '');
    if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
  }
  if (data.length === 0 || (event !== 'tick' && event !== 'status')) return undefined;
  return { event, data: JSON.parse(data.join('\n')) } as StreamEvent;
}
