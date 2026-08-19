import type { BacktestReport, BacktestRequest, CandlesResponse, ServiceConfig } from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function headers(): HeadersInit {
  const key = import.meta.env.VITE_API_KEY;
  const next: Record<string, string> = { Accept: "application/json" };
  if (typeof key === "string" && key.length > 0) {
    next["X-API-Key"] = key;
  }
  return next;
}

function detailMessage(status: number, body: unknown): string {
  if (status === 401) return "Set VITE_API_KEY to this service’s API_KEY";
  if (status === 404) return "No candles for that range and source";
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return `Request failed (${status})`;
}

async function parse(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { ...headers(), ...(init?.headers ?? {}) },
  });
  const body = await parse(response);
  if (!response.ok) {
    throw new ApiError(detailMessage(response.status, body), response.status);
  }
  return body as T;
}

export function fetchConfig(): Promise<ServiceConfig> {
  return request<ServiceConfig>("/v1/config");
}

export function runBacktest(body: BacktestRequest): Promise<BacktestReport> {
  return request<BacktestReport>("/v1/backtests", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function fetchCandles(params: {
  symbol: string;
  timeframe: string;
  count: number;
  to?: string | null;
  source?: string | null;
}): Promise<CandlesResponse> {
  const query = new URLSearchParams({
    symbol: params.symbol,
    timeframe: params.timeframe,
    count: String(params.count),
  });
  if (params.to) query.set("to", params.to);
  if (params.source && params.source !== "auto") query.set("source", params.source);
  return request<CandlesResponse>(`/v1/candles?${query.toString()}`);
}
