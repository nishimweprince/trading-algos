/** Trading session bands — mirrors server `context.session_from_ts`. */
export type TradingSession = "asian" | "london" | "ny" | "off_hours";

const SESSION_LABELS: Record<TradingSession, string> = {
  asian: "Asian",
  london: "London",
  ny: "New York",
  off_hours: "Off hours",
};

export function sessionFromTs(ts: string): TradingSession {
  const hour = new Date(ts).getUTCHours();
  if (hour >= 0 && hour < 7) return "asian";
  if (hour >= 7 && hour < 13) return "london";
  if (hour >= 13 && hour < 21) return "ny";
  return "off_hours";
}

export function formatTradingSession(session: TradingSession): string {
  return SESSION_LABELS[session];
}

export const TRADING_SESSIONS: TradingSession[] = ["asian", "london", "ny", "off_hours"];
