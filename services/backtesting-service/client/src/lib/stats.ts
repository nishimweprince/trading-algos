import {
  SESSION_LABEL,
  type ClosedLeg,
  type EngineEvent,
  type PerformanceUnit,
  type TradePairResult,
} from "./types";

export type PairSortKey = "entry_ts" | "session" | "entry" | "pnl" | "status";
export type SortDir = "asc" | "desc";

const STATUS_RANK: Record<TradePairResult["status"], number> = {
  open: 0,
  partial: 1,
  closed: 2,
};

function pairSortValue(
  pair: TradePairResult,
  key: PairSortKey,
  unit: PerformanceUnit,
): number | string {
  switch (key) {
    case "entry_ts":
      return Date.parse(pair.entry_ts) || 0;
    case "session":
      return SESSION_LABEL[pair.session] ?? pair.session;
    case "entry":
      return pair.entry;
    case "pnl":
      return unit === "dollars" && pair.pnl_dollars !== null
        ? pair.pnl_dollars
        : (pair.net_pnl_pips ?? pair.gross_pnl_pips ?? pair.pnl_pips);
    case "status":
      return STATUS_RANK[pair.status];
  }
}

export function sortPairs(
  pairs: TradePairResult[],
  key: PairSortKey,
  dir: SortDir,
  unit: PerformanceUnit,
): TradePairResult[] {
  const sign = dir === "asc" ? 1 : -1;
  return [...pairs].sort((a, b) => {
    const left = pairSortValue(a, key, unit);
    const right = pairSortValue(b, key, unit);
    let cmp = 0;
    if (typeof left === "string" && typeof right === "string") {
      cmp = left.localeCompare(right);
    } else {
      cmp = (left as number) - (right as number);
    }
    if (cmp === 0 && key !== "entry_ts") {
      return (Date.parse(b.entry_ts) || 0) - (Date.parse(a.entry_ts) || 0);
    }
    return cmp * sign;
  });
}

export function closedCount(wins: number, be: number, loss: number): number {
  return wins + be + loss;
}

export function winRate(wins: number, be: number, loss: number): number | null {
  const total = closedCount(wins, be, loss);
  if (total === 0) return null;
  return wins / total;
}

export function winRateExclBe(wins: number, be: number, loss: number): number | null {
  void be;
  const directional = wins + loss;
  if (directional === 0) return null;
  return wins / directional;
}

export function filterBySession<T extends { session: string }>(
  items: T[],
  session: string | null,
): T[] {
  if (session === null) return items;
  return items.filter((item) => item.session === session);
}

export type SessionBucket = {
  session: string;
  wins: number;
  be: number;
  loss: number;
  pnl: number;
};

export function sessionBreakdown(trades: ClosedLeg[]): SessionBucket[] {
  const map = new Map<string, SessionBucket>();
  for (const trade of trades) {
    const row = map.get(trade.session) ?? {
      session: trade.session,
      wins: 0,
      be: 0,
      loss: 0,
      pnl: 0,
    };
    if (trade.bucket === "win") row.wins += 1;
    else if (trade.bucket === "be") row.be += 1;
    else row.loss += 1;
    row.pnl += trade.pnl;
    map.set(trade.session, row);
  }
  const preferred = ["tokyo", "london", "new_york"];
  const ordered: SessionBucket[] = [];
  for (const name of preferred) {
    const row = map.get(name);
    if (row) ordered.push(row);
  }
  for (const [name, row] of map) {
    if (!preferred.includes(name)) ordered.push(row);
  }
  return ordered;
}

export function pairSessionBreakdown(
  pairs: TradePairResult[],
  unit: PerformanceUnit,
): SessionBucket[] {
  const map = new Map<string, SessionBucket>();
  for (const pair of pairs) {
    const row = map.get(pair.session) ?? {
      session: pair.session,
      wins: 0,
      be: 0,
      loss: 0,
      pnl: 0,
    };
    row.pnl +=
      unit === "dollars" && pair.pnl_dollars !== null
        ? pair.pnl_dollars
        : (pair.net_pnl_pips ?? pair.gross_pnl_pips ?? pair.pnl_pips);
    for (const leg of [pair.primary, pair.hedge, ...pair.unknown_legs]) {
      if (!leg || leg.status !== "closed" || leg.bucket === null) continue;
      if (leg.bucket === "win") row.wins += 1;
      else if (leg.bucket === "be") row.be += 1;
      else row.loss += 1;
    }
    map.set(pair.session, row);
  }
  const ordered: SessionBucket[] = [];
  for (const name of ["tokyo", "london", "new_york"]) {
    const row = map.get(name);
    if (row) ordered.push(row);
  }
  for (const [name, row] of map) {
    if (!["tokyo", "london", "new_york"].includes(name)) ordered.push(row);
  }
  return ordered;
}

export function markerEvents(events: EngineEvent[], session: string | null): EngineEvent[] {
  return filterBySession(events, session).filter(
    (event) => event.kind === "entry" || event.kind === "exit",
  );
}

export type HistogramBucket = { label: string; count: number };

export function rHistogram(pairs: TradePairResult[]): HistogramBucket[] {
  const buckets = [
    { label: "<−2R", min: -Infinity, max: -2 },
    { label: "−2…−1R", min: -2, max: -1 },
    { label: "−1…0R", min: -1, max: 0 },
    { label: "0…1R", min: 0, max: 1 },
    { label: "1…2R", min: 1, max: 2 },
    { label: "≥2R", min: 2, max: Infinity },
  ];
  return buckets.map(({ label, min, max }) => ({
    label,
    count: pairs.filter((pair) => pair.net_r != null && pair.net_r >= min && pair.net_r < max)
      .length,
  }));
}

export function holdingDistribution(pairs: TradePairResult[]): HistogramBucket[] {
  const buckets = [
    { label: "<1h", min: 0, max: 1 },
    { label: "1–4h", min: 1, max: 4 },
    { label: "4–12h", min: 4, max: 12 },
    { label: "12–24h", min: 12, max: 24 },
    { label: "≥24h", min: 24, max: Infinity },
  ];
  return buckets.map(({ label, min, max }) => ({
    label,
    count: pairs.filter(
      (pair) => pair.hold_hours != null && pair.hold_hours >= min && pair.hold_hours < max,
    ).length,
  }));
}

export type PerformanceBucket = {
  label: string;
  structures: number;
  grossPips: number;
  netPips: number;
  grossR: number;
  netR: number;
};

export function performanceBreakdown(
  pairs: TradePairResult[],
  key: "session" | "weekday",
): PerformanceBucket[] {
  const map = new Map<string, PerformanceBucket>();
  for (const pair of pairs.filter((item) => item.status === "closed")) {
    const label = (key === "session" ? pair.session : pair.weekday) ?? "unknown";
    const row = map.get(label) ?? {
      label,
      structures: 0,
      grossPips: 0,
      netPips: 0,
      grossR: 0,
      netR: 0,
    };
    row.structures += 1;
    row.grossPips += pair.gross_pnl_pips ?? pair.pnl_pips;
    row.netPips += pair.net_pnl_pips ?? pair.gross_pnl_pips ?? pair.pnl_pips;
    row.grossR += pair.gross_r ?? 0;
    row.netR += pair.net_r ?? pair.gross_r ?? 0;
    map.set(label, row);
  }
  const preferred =
    key === "session"
      ? ["tokyo", "london", "new_york"]
      : ["monday", "tuesday", "wednesday", "thursday", "friday"];
  return [...map.values()].sort((a, b) => {
    const ai = preferred.indexOf(a.label);
    const bi = preferred.indexOf(b.label);
    return (ai < 0 ? preferred.length : ai) - (bi < 0 ? preferred.length : bi);
  });
}

export type ExcursionPoint = { mae: number; mfe: number; side: "long" | "short" };

export function excursionPoints(pairs: TradePairResult[]): ExcursionPoint[] {
  return pairs.flatMap((pair) =>
    [pair.primary, pair.hedge, ...pair.unknown_legs]
      .filter((leg) => leg?.status === "closed")
      .map((leg) => ({ mae: leg!.mae_pips, mfe: leg!.mfe_pips, side: leg!.side })),
  );
}

export type ConcurrencyPoint = { ts: string; count: number };

export function concurrencyTimeline(pairs: TradePairResult[]): ConcurrencyPoint[] {
  const events: { ts: string; delta: number }[] = [];
  for (const pair of pairs) {
    events.push({ ts: pair.entry_ts, delta: 1 });
    const exits = [pair.primary, pair.hedge, ...pair.unknown_legs]
      .map((leg) => leg?.exit_ts)
      .filter((ts): ts is string => ts != null);
    if (pair.status === "closed" && exits.length > 0) {
      events.push({ ts: exits.sort((a, b) => Date.parse(b) - Date.parse(a))[0], delta: -1 });
    }
  }
  events.sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts) || a.delta - b.delta);
  let count = 0;
  return events.map((event) => ({ ts: event.ts, count: (count += event.delta) }));
}
