import { toUtcIso } from "@/lib/format";
import type { TradeProvenance, TradeSubmit } from "@/types";

export interface SkipTradeInput {
  session_id: string;
  symbol: string;
  timeframe: string;
  setup_id: string;
  side: 1 | -1;
  skip_reason: string;
  signal_ts: string;
  signal_id?: string | null;
  entry?: number | null;
  sl?: number | null;
  tp?: number | null;
  notes?: string;
  calendar_flag?: boolean;
  calendar_tags?: string;
  blinded?: boolean;
  provenance?: TradeProvenance | null;
  screenshot_entry?: string;
}

/** Resolve which bar a skip should attach to: bookmark wins over the live cursor. */
export function resolveSkipSignalTs(
  bookmarkTs: string | null,
  currentBarTs: string | null,
): string | null {
  if (bookmarkTs) return bookmarkTs;
  if (currentBarTs) return toUtcIso(currentBarTs);
  return null;
}

export function buildSkipPayload(input: SkipTradeInput): TradeSubmit {
  return {
    session_id: input.session_id,
    signal_id: input.signal_id ?? undefined,
    symbol: input.symbol,
    timeframe: input.timeframe,
    signal_ts: input.signal_ts,
    setup_id: input.setup_id,
    side: input.side,
    outcome_kind: "skipped",
    skip_reason: input.skip_reason,
    entry: input.entry ?? undefined,
    sl: input.sl ?? undefined,
    tp: input.tp ?? undefined,
    notes: input.notes || undefined,
    calendar_flag: input.calendar_flag,
    calendar_tags: input.calendar_tags || undefined,
    blinded: input.blinded,
    provenance: input.provenance ?? undefined,
    screenshot_entry: input.screenshot_entry,
  };
}
