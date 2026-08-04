import { describe, expect, it } from "vitest";
import { buildSkipPayload, resolveSkipSignalTs } from "./skipTrade";

describe("resolveSkipSignalTs", () => {
  it("prefers bookmark timestamp over cursor bar", () => {
    expect(resolveSkipSignalTs("2026-05-01T10:00:00Z", "2026-05-01T12:00:00Z")).toBe(
      "2026-05-01T10:00:00Z",
    );
  });

  it("falls back to current bar when no bookmark", () => {
    expect(resolveSkipSignalTs(null, "2026-05-01T12:00:00.000Z")).toBe("2026-05-01T12:00:00.000Z");
  });

  it("returns null when neither bookmark nor cursor is set", () => {
    expect(resolveSkipSignalTs(null, null)).toBeNull();
  });
});

describe("buildSkipPayload", () => {
  it("builds a skipped trade submit body", () => {
    const payload = buildSkipPayload({
      session_id: "sess-1",
      symbol: "XAUUSD",
      timeframe: "H1",
      setup_id: "head_shoulders",
      side: -1,
      skip_reason: "missed_entry",
      signal_ts: "2026-05-01T10:00:00Z",
    });

    expect(payload.outcome_kind).toBe("skipped");
    expect(payload.skip_reason).toBe("missed_entry");
    expect(payload.signal_ts).toBe("2026-05-01T10:00:00Z");
    expect(payload.entry).toBeUndefined();
  });
});
