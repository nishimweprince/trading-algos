const API_BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json() as Promise<T>;
}

export const api = {
  getSymbols: () => request<string[]>("/symbols"),
  getTimeframes: (symbol: string) =>
    request<string[]>(`/timeframes?symbol=${encodeURIComponent(symbol)}`),
  getCandles: (symbol: string, timeframe: string, dateFrom: string, dateTo: string) =>
    request<import("@/types").Candle[]>(
      `/candles?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&date_from=${encodeURIComponent(dateFrom)}&date_to=${encodeURIComponent(dateTo)}`,
    ),
  getSetups: () => request<import("@/types").Setup[]>("/setups"),
  createSession: (body: import("@/types").SessionCreate) =>
    request<import("@/types").Session>("/sessions", { method: "POST", body: JSON.stringify(body) }),
  submitTrade: (body: import("@/types").TradeSubmit) =>
    request<import("@/types").Occurrence>("/trades", { method: "POST", body: JSON.stringify(body) }),
  getTrades: (sessionId?: string) =>
    request<import("@/types").Occurrence[]>(
      sessionId ? `/trades?session_id=${encodeURIComponent(sessionId)}` : "/trades",
    ),
  compare: (body: {
    setup_id: string;
    symbol: string;
    timeframe: string;
    context: import("@/types").CompareContext;
    source?: string;
    min_samples?: number;
  }) =>
    request<import("@/types").CompareResult>("/compare", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  uploadScreenshot: (body: {
    session_id: string;
    trade_id?: string;
    kind: "entry" | "exit";
    image_base64: string;
  }) =>
    request<{ path: string; trade_id: string }>("/screenshots", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  screenshotUrl: (path: string) => {
    const parts = path.replace(/^screenshots\//, "").split("/");
    if (parts.length !== 2) return null;
    return `${API_BASE}/screenshots/${encodeURIComponent(parts[0])}/${encodeURIComponent(parts[1])}`;
  },
};
