const API_BASE = "/api";

interface ValidationIssue {
  loc?: (string | number)[];
  msg?: string;
}

/**
 * FastAPI returns a plain string `detail` for HTTPException but an array of
 * issues for a 422. Interpolating the array gave "[object Object]", which hid
 * which field was actually rejected.
 */
function formatDetail(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const issues = (detail as ValidationIssue[])
      .map((issue) => {
        // loc is ["body", "side"]; the field name is the informative part.
        const field = issue.loc?.filter((p) => p !== "body").join(".");
        return field ? `${field}: ${issue.msg ?? "invalid"}` : issue.msg;
      })
      .filter(Boolean);
    if (issues.length > 0) return issues.join("; ");
  }
  return fallback;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(formatDetail(err.detail, res.statusText));
  }
  return res.json() as Promise<T>;
}

export const api = {
  getSymbols: () => request<string[]>("/symbols"),
  getTimeframes: (symbol: string) =>
    request<string[]>(`/timeframes?symbol=${encodeURIComponent(symbol)}`),
  getCandleBounds: (symbol: string, timeframe: string) =>
    request<import("@/types").CandleBounds>(
      `/candles/bounds?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}`,
    ),
  getCandles: (symbol: string, timeframe: string, dateFrom: string, dateTo: string) =>
    request<import("@/types").Candle[]>(
      `/candles?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&date_from=${encodeURIComponent(dateFrom)}&date_to=${encodeURIComponent(dateTo)}`,
    ),
  getSetups: () => request<import("@/types").Setup[]>("/setups"),
  getContext: (symbol: string, timeframe: string, signalTs: string) =>
    request<import("@/types").SignalContext>(
      `/context?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&signal_ts=${encodeURIComponent(signalTs)}`,
    ),
  getBaseRate: (q: import("@/types").BaseRateQuery) => {
    const params = new URLSearchParams({
      symbol: q.symbol,
      timeframe: q.timeframe,
      signal_ts: q.signalTs,
    });
    if (q.horizon != null) params.set("horizon", String(q.horizon));
    if (q.targetAtr != null) params.set("target_atr", String(q.targetAtr));
    if (q.stopAtr != null) params.set("stop_atr", String(q.stopAtr));
    if (q.side != null) params.set("side", String(q.side));
    if (q.minSamples != null) params.set("min_samples", String(q.minSamples));
    for (const pin of q.pinned ?? []) params.append("pinned", pin);
    params.set("apply_cost", "true");
    return request<import("@/types").BaseRate>(`/base-rate?${params}`);
  },
  getBarSeries: (q: import("@/types").BarSeriesQuery) => {
    const params = new URLSearchParams({
      symbol: q.symbol,
      timeframe: q.timeframe,
      date_from: q.dateFrom,
      date_to: q.dateTo,
    });
    // Omitted rather than defaulted: without it the server returns no forward
    // data at all, which is the safe failure.
    if (q.revealedThrough) params.set("revealed_through", q.revealedThrough);
    if (q.horizon != null) params.set("horizon", String(q.horizon));
    return request<import("@/types").BarFeatureRow[]>(`/bar-features/series?${params}`);
  },
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
    pinned?: string[];
    source?: string;
    min_samples?: number;
    exclude_peeked?: boolean;
    exclude_assisted?: boolean;
    blinded_only?: boolean;
    break_even_win_rate?: number;
  }) =>
    request<import("@/types").CompareResult>("/compare", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  submitSignal: (body: import("@/types").SignalSubmit) =>
    request<import("@/types").Signal>("/signals", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getSignals: (sessionId?: string) =>
    request<import("@/types").Signal[]>(
      sessionId ? `/signals?session_id=${encodeURIComponent(sessionId)}` : "/signals",
    ),
  resolvePendingSignals: () =>
    request<{ resolved_count: number; resolved_ids: string[] }>("/signals/resolve-pending", {
      method: "POST",
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
  getCalendarFlags: (symbol: string, ts: string) =>
    request<{ high_impact_today: boolean; events: { time_utc: string; currency: string; impact: string; title: string }[] }>(
      `/calendar/flags?symbol=${encodeURIComponent(symbol)}&ts=${encodeURIComponent(ts)}`,
    ),
};
