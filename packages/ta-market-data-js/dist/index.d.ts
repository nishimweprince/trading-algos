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
export type Timeframe = 'M1' | 'M2' | 'M3' | 'M4' | 'M5' | 'M10' | 'M15' | 'M30' | 'H1' | 'H4' | 'H12' | 'D1' | 'W1';
export declare const TIMEFRAME_MINUTES: Record<Timeframe, number>;
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
export type StreamEvent = {
    event: 'tick';
    data: MarketQuote;
} | {
    event: 'status';
    data: StreamStatus;
};
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
export declare class MarketDataError extends Error {
    readonly status: number;
    readonly code: string;
    readonly details: unknown;
    constructor(status: number, code: string, message: string, details?: unknown);
}
export interface Subscription {
    close(): void;
    /** Settles when the stream ends: closed, or failed (rejects). */
    done: Promise<void>;
}
export declare class MarketDataClient {
    readonly market: Market;
    private readonly base;
    private readonly apiKey;
    private readonly provider?;
    private readonly timeoutMs;
    private readonly pageSize;
    private readonly fetchImpl;
    constructor(options: MarketDataClientOptions);
    quote(symbol: string): Promise<MarketQuote>;
    instruments(): Promise<InstrumentInfo[]>;
    capabilities(): Promise<Capabilities>;
    /** Up to `count` closed bars ending at or before `to` (default now), oldest first. */
    candles(symbol: string, timeframe: Timeframe, count: number, options?: {
        to?: Date | string;
    }): Promise<Candle[]>;
    /** Every closed bar whose interval ends in [from, to]. */
    candlesRange(symbol: string, timeframe: Timeframe, from: Date | string, to?: Date | string): Promise<Candle[]>;
    /** The service's own readiness, which includes quote staleness. */
    ready(): Promise<{
        ready: boolean;
        reason: string;
    }>;
    /**
     * Live quotes over SSE. The service replays the newest quote per symbol on
     * connect, then pushes each change; `status` events report the upstream
     * connection and how many ticks this consumer dropped by being slow.
     */
    stream(symbols: string[] | undefined, onEvent: (event: StreamEvent) => void): Subscription;
    private headers;
    private url;
    private get;
}
