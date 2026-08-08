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
  if (
    detail != null &&
    typeof detail === "object" &&
    "message" in detail &&
    typeof detail.message === "string"
  ) {
    return detail.message;
  }
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
  getCandlePage: (
    symbol: string,
    timeframe: string,
    dateFrom: string,
    dateTo: string,
    offset = 0,
    limit = 2048,
  ) => {
    const params = new URLSearchParams({
      symbol,
      timeframe,
      date_from: dateFrom,
      date_to: dateTo,
      offset: String(offset),
      limit: String(limit),
    });
    return request<import("@/types").CandlePage>(`/candles/page?${params}`);
  },
  getMetaEvents: (query: import("@/types").MetaEventQuery) => {
    const params = new URLSearchParams({
      symbol: query.symbol,
      timeframe: query.timeframe,
      offset: String(query.offset ?? 0),
      limit: String(query.limit ?? 100),
    });
    if (query.year != null) params.set("year", String(query.year));
    if (query.setup) params.set("setup", query.setup);
    if (query.side != null) params.set("side", String(query.side));
    if (query.confidenceMin != null) params.set("confidence_min", String(query.confidenceMin));
    if (query.quality) params.set("quality", query.quality);
    if (query.reviewStatus) params.set("review_status", query.reviewStatus);
    return request<import("@/types").MetaEventPage>(`/meta-events?${params}`);
  },
  getMetaEventSummary: (symbol: string, timeframe: string) =>
    request<import("@/types").MetaEventSummary>(
      `/meta-events/summary?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}`,
    ),
  getMetaEvent: (eventId: string) =>
    request<import("@/types").MetaEventDetail>(`/meta-events/${encodeURIComponent(eventId)}`),
  reviewMetaEvent: (
    eventId: string,
    body: { verdict?: import("@/types").MetaEventVerdict; notes?: string; phase?: "pre" | "post" },
  ) =>
    request<import("@/types").MetaEventDetail>(
      `/meta-events/${encodeURIComponent(eventId)}/review`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  revealMetaEvent: (eventId: string) =>
    request<import("@/types").MetaEventOutcome>(
      `/meta-events/${encodeURIComponent(eventId)}/reveal`,
      { method: "POST" },
    ),
  getMetaShadow: (symbol: string, timeframe: string, signalTs: string) => {
    const params = new URLSearchParams({ symbol, timeframe, signal_ts: signalTs });
    return request<import("@/types").MetaShadowEvent>(`/meta-model/shadow?${params}`);
  },
  getMetaShadowHistory: (
    symbol: string,
    timeframe: string,
    offset = 0,
    limit = 100,
    asOf?: string,
  ) => {
    const params = new URLSearchParams({
      symbol,
      timeframe,
      offset: String(offset),
      limit: String(limit),
    });
    if (asOf) params.set("as_of", asOf);
    return request<import("@/types").MetaShadowPage>(`/meta-model/shadow/history?${params}`);
  },
  getMetaModelStatus: () => request<import("@/types").MetaModelStatus>("/meta-model/status"),
  getMetaReplayShadow: (symbol: string, timeframe: string, signalTs: string, side: 1 | -1) => {
    const params = new URLSearchParams({
      symbol,
      timeframe,
      signal_ts: signalTs,
      side: String(side),
    });
    return request<import("@/types").MetaReplayInference>(`/meta-model/replay?${params}`);
  },
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
    if (q.tagSetupId) params.set("tag_setup_id", q.tagSetupId);
    params.set("tag_state", q.tagState ?? "complete");
    for (const pin of q.pinned ?? []) params.append("pinned", pin);
    params.set("apply_cost", "true");
    return request<import("@/types").BaseRate>(`/base-rate?${params}`);
  },
  getOutcomeShadow: (symbol: string, timeframe: string, signalTs: string) => {
    const params = new URLSearchParams({
      symbol,
      timeframe,
      signal_ts: signalTs,
    });
    return request<import("@/types").OutcomeShadow>(`/outcome-model/shadow?${params}`);
  },
  getShadowHistory: (
    symbol: string,
    timeframe: string,
    dateFrom: string,
    dateTo: string,
    revealedThrough?: string,
  ) => {
    const params = new URLSearchParams({
      symbol,
      timeframe,
      date_from: dateFrom,
      date_to: dateTo,
    });
    if (revealedThrough) params.set("revealed_through", revealedThrough);
    return request<import("@/types").ShadowPrediction[]>(
      `/outcome-model/shadow/history?${params}`,
    );
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
