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
        : pair.pnl_pips;
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
        : pair.pnl_pips;
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
    (event) => event.kind === "entry" || event.kind === "exit" || event.kind === "skip",
  );
}

export interface SkipSummary {
  count: number;
  reasons: string[];
}

export function skipSummary(events: EngineEvent[]): SkipSummary {
  const counts = new Map<string, number>();
  for (const event of events) {
    if (event.kind !== "skip") continue;
    const reason = String(event.detail.reason ?? "unknown");
    counts.set(reason, (counts.get(reason) ?? 0) + 1);
  }
  const ranked = [...counts.entries()].sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]));
  const reasons =
    ranked.length === 1
      ? [ranked[0][0]]
      : ranked.map(([reason, count]) => `${reason} ${count}`);
  return { count: [...counts.values()].reduce((sum, n) => sum + n, 0), reasons };
}

export function formatSkipSummary(summary: SkipSummary): string {
  if (summary.count === 0) return "";
  const reason = summary.reasons.join(" · ");
  return reason ? `${summary.count} skipped · ${reason}` : `${summary.count} skipped`;
}
