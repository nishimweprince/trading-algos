"use strict";
/**
 * Client for market-data-service, the only place JavaScript consumers should
 * get prices from. It never talks to an exchange or broker itself.
 *
 * Mirrors the Python `ta_clients.MarketDataClient`: one instance per market
 * (`forex`, `deriv`, `crypto`), candles are closed bars stamped at the UTC END
 * of their interval, and large candle requests page backwards on `to`.
 * Runtime dependencies: none (global `fetch`, Node 18.17+).
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.MarketDataClient = exports.MarketDataError = exports.TIMEFRAME_MINUTES = void 0;
exports.TIMEFRAME_MINUTES = {
    M1: 1, M2: 2, M3: 3, M4: 4, M5: 5, M10: 10, M15: 15, M30: 30,
    H1: 60, H4: 240, H12: 720, D1: 1440, W1: 10080,
};
/** A non-2xx answer, carrying the service's structured error. */
class MarketDataError extends Error {
    status;
    code;
    details;
    constructor(status, code, message, details) {
        super(message);
        this.name = 'MarketDataError';
        this.status = status;
        this.code = code;
        this.details = details;
    }
}
exports.MarketDataError = MarketDataError;
class MarketDataClient {
    market;
    base;
    apiKey;
    provider;
    timeoutMs;
    pageSize;
    fetchImpl;
    constructor(options) {
        this.base = options.baseUrl.replace(/\/+$/, '');
        this.apiKey = options.apiKey;
        this.market = options.market;
        this.provider = options.provider;
        this.timeoutMs = options.timeoutMs ?? 10_000;
        this.pageSize = options.pageSize ?? 1000;
        const impl = options.fetch ?? globalThis.fetch;
        if (!impl)
            throw new Error('global fetch is unavailable; use Node 18.17 or newer');
        this.fetchImpl = impl;
    }
    async quote(symbol) {
        return this.get('tick', { symbol });
    }
    async instruments() {
        const body = await this.get('symbols', {});
        return body.instruments;
    }
    async capabilities() {
        return this.get('capabilities', {});
    }
    /** Up to `count` closed bars ending at or before `to` (default now), oldest first. */
    async candles(symbol, timeframe, count, options = {}) {
        const collected = new Map();
        let cursor = options.to === undefined ? undefined : toIso(options.to);
        while (collected.size < count) {
            // `to` is inclusive, so a page after the first repeats the bar at the
            // cursor; ask for one more so the overlap does not cost a bar.
            const overlap = cursor === undefined ? 0 : 1;
            const take = Math.min(this.pageSize, count - collected.size + overlap);
            const params = { symbol, timeframe, count: String(take) };
            if (cursor !== undefined)
                params.to = cursor;
            const page = await this.get('candles', params);
            const fresh = page.candles.filter((candle) => !collected.has(candle.ts));
            if (fresh.length === 0)
                break;
            for (const candle of fresh)
                collected.set(candle.ts, candle);
            const oldest = [...collected.keys()].sort(byTime)[0];
            if (cursor !== undefined && Date.parse(oldest) >= Date.parse(cursor))
                break;
            cursor = oldest;
        }
        return [...collected.values()].sort((a, b) => byTime(a.ts, b.ts)).slice(-count);
    }
    /** Every closed bar whose interval ends in [from, to]. */
    async candlesRange(symbol, timeframe, from, to) {
        const start = Date.parse(toIso(from));
        const end = to === undefined ? Date.now() : Date.parse(toIso(to));
        const minutes = exports.TIMEFRAME_MINUTES[timeframe];
        const count = Math.floor(Math.max((end - start) / 60_000, minutes) / minutes) + 8;
        const candles = await this.candles(symbol, timeframe, count, to === undefined ? {} : { to });
        return candles.filter((candle) => {
            const ts = Date.parse(candle.ts);
            return ts >= start && ts <= end;
        });
    }
    /** The service's own readiness, which includes quote staleness. */
    async ready() {
        try {
            const response = await this.fetchImpl(`${this.base}/health/ready`, {
                signal: AbortSignal.timeout(5_000),
            });
            return response.ok
                ? { ready: true, reason: 'ok' }
                : { ready: false, reason: `status ${response.status}` };
        }
        catch (error) {
            return { ready: false, reason: error.message };
        }
    }
    /**
     * Live quotes over SSE. The service replays the newest quote per symbol on
     * connect, then pushes each change; `status` events report the upstream
     * connection and how many ticks this consumer dropped by being slow.
     */
    stream(symbols, onEvent) {
        const controller = new AbortController();
        const params = {};
        if (symbols && symbols.length > 0)
            params.symbols = symbols.join(',');
        const url = this.url('stream/ticks', params);
        const done = (async () => {
            const response = await this.fetchImpl(url, {
                headers: { ...this.headers(), Accept: 'text/event-stream' },
                signal: controller.signal,
            });
            if (!response.ok || !response.body)
                throw await toError(response);
            const decoder = new TextDecoder();
            let buffer = '';
            const reader = response.body.getReader();
            try {
                for (;;) {
                    const { value, done: finished } = await reader.read();
                    if (finished)
                        break;
                    buffer += decoder.decode(value, { stream: true });
                    let boundary;
                    while ((boundary = buffer.search(/\r?\n\r?\n/)) !== -1) {
                        const block = buffer.slice(0, boundary);
                        buffer = buffer.slice(boundary).replace(/^\r?\n\r?\n/, '');
                        const parsed = parseSse(block);
                        if (parsed)
                            onEvent(parsed);
                    }
                }
            }
            catch (error) {
                if (!controller.signal.aborted)
                    throw error;
            }
        })().catch((error) => {
            if (controller.signal.aborted)
                return;
            throw error;
        });
        return { close: () => controller.abort(), done };
    }
    // --- transport -------------------------------------------------------------
    headers() {
        return { 'X-API-Key': this.apiKey };
    }
    url(route, params) {
        const query = new URLSearchParams(params);
        if (this.provider)
            query.set('provider', this.provider);
        const qs = query.toString();
        return `${this.base}/v1/${this.market}/${route}${qs ? `?${qs}` : ''}`;
    }
    async get(route, params) {
        const response = await this.fetchImpl(this.url(route, params), {
            headers: this.headers(),
            signal: AbortSignal.timeout(this.timeoutMs),
        });
        if (!response.ok)
            throw await toError(response);
        return (await response.json());
    }
}
exports.MarketDataClient = MarketDataClient;
function toIso(value) {
    return value instanceof Date ? value.toISOString() : new Date(value).toISOString();
}
function byTime(a, b) {
    return Date.parse(a) - Date.parse(b);
}
async function toError(response) {
    let body;
    try {
        body = await response.json();
    }
    catch {
        body = undefined;
    }
    const error = body
        ?.error;
    return new MarketDataError(response.status, error?.code ?? 'http_error', error?.message ?? `market-data-service answered ${response.status}`, error?.details);
}
function parseSse(block) {
    let event = 'message';
    const data = [];
    for (const line of block.split(/\r?\n/)) {
        if (line.startsWith(':'))
            continue; // keepalive comment
        const colon = line.indexOf(':');
        const field = colon === -1 ? line : line.slice(0, colon);
        const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '');
        if (field === 'event')
            event = value;
        else if (field === 'data')
            data.push(value);
    }
    if (data.length === 0 || (event !== 'tick' && event !== 'status'))
        return undefined;
    return { event, data: JSON.parse(data.join('\n')) };
}
