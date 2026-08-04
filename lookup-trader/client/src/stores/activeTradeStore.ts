import { create } from "zustand";
import { pipsCaptured, rMultiple } from "@/lib/pips";
import { sessionFromTs, type TradingSession } from "@/lib/tradingSession";
import type { LiveResult } from "@/lib/barrier";
import type { Candle } from "@/types";

export type TradeStatus = "idle" | "active" | "resolved";

export interface StartTradeParams {
  signalIdx: number;
  signalTs: string;
  setup_id: string;
  side: 1 | -1;
  entry: number;
  sl: number;
  tp: number;
  symbol: string;
  calendar_flag?: boolean;
  calendar_tags?: string;
}

/** How the operator got to this label. Mirrors the server's TradeProvenance. */
export interface TradeProvenance {
  peeked: boolean;
  max_cursor_before_arm: number;
  decision_ms: number;
  level_revisions: number;
  bars_visible_at_signal: number;
}

interface ActiveTradeState {
  status: TradeStatus;
  signalIdx: number;
  signalTs: string;
  setup_id: string;
  side: 1 | -1;
  entry: number;
  sl: number;
  tp: number;
  symbol: string;
  tradingSession: TradingSession;
  calendar_flag: boolean;
  calendar_tags: string;
  exitIdx: number | null;
  exitPrice: number | null;
  liveResult: LiveResult | null;
  pips: number;
  unrealizedR: number;
  barsInTrade: number;
  entryScreenshotBlob: Blob | null;
  exitScreenshotBlob: Blob | null;
  entryScreenshotPath: string | null;
  exitScreenshotPath: string | null;
  lastCheckedIdx: number;
  draftTradeId: string;
  /** Level edits since the last submit — a proxy for how settled the read was. */
  levelRevisions: number;
  provenance: TradeProvenance | null;

  startTrade: (params: StartTradeParams) => string;
  noteLevelRevision: () => void;
  setProvenance: (provenance: TradeProvenance) => void;
  updateLive: (cursor: number, bar: Candle, size: number) => void;
  resolveTrade: (result: LiveResult, exitIdx: number, exitPrice: number, pips: number) => void;
  setEntryScreenshot: (blob: Blob) => void;
  setExitScreenshot: (blob: Blob) => void;
  setScreenshotPaths: (entry: string | null, exit: string | null) => void;
  setLastCheckedIdx: (idx: number) => void;
  cancel: () => void;
  reset: () => void;
  getMinCursor: () => number;
}

const INITIAL: Omit<
  ActiveTradeState,
  | "startTrade"
  | "noteLevelRevision"
  | "setProvenance"
  | "updateLive"
  | "resolveTrade"
  | "setEntryScreenshot"
  | "setExitScreenshot"
  | "setScreenshotPaths"
  | "setLastCheckedIdx"
  | "cancel"
  | "reset"
  | "getMinCursor"
> = {
  status: "idle",
  signalIdx: 0,
  signalTs: "",
  setup_id: "",
  side: 1,
  entry: 0,
  sl: 0,
  tp: 0,
  symbol: "",
  tradingSession: "off_hours",
  calendar_flag: false,
  calendar_tags: "",
  exitIdx: null,
  exitPrice: null,
  liveResult: null,
  pips: 0,
  unrealizedR: 0,
  barsInTrade: 0,
  entryScreenshotBlob: null,
  exitScreenshotBlob: null,
  entryScreenshotPath: null,
  exitScreenshotPath: null,
  lastCheckedIdx: 0,
  draftTradeId: "",
  levelRevisions: 0,
  provenance: null,
};

export const useActiveTradeStore = create<ActiveTradeState>((set, get) => ({
  ...INITIAL,

  startTrade: (params) => {
    const draftTradeId = crypto.randomUUID();
    set({
      status: "active",
      ...params,
      tradingSession: sessionFromTs(params.signalTs),
      calendar_flag: params.calendar_flag ?? false,
      calendar_tags: params.calendar_tags ?? "",
      exitIdx: null,
      exitPrice: null,
      liveResult: null,
      pips: 0,
      unrealizedR: 0,
      barsInTrade: 0,
      entryScreenshotBlob: null,
      exitScreenshotBlob: null,
      entryScreenshotPath: null,
      exitScreenshotPath: null,
      lastCheckedIdx: params.signalIdx,
      draftTradeId,
    });
    return draftTradeId;
  },

  // Counted from the moment the operator starts marking levels, and cleared only
  // on reset — a trade re-marked five times is a different signal from a snap read.
  noteLevelRevision: () => set((s) => ({ levelRevisions: s.levelRevisions + 1 })),
  setProvenance: (provenance) => set({ provenance }),

  updateLive: (cursor, bar, size) => {
    const { signalIdx, side, entry, sl } = get();
    const mark = bar.close;
    set({
      barsInTrade: Math.max(0, cursor - signalIdx),
      pips: pipsCaptured(side, entry, mark, size),
      unrealizedR: rMultiple(side, entry, sl, mark),
    });
  },

  resolveTrade: (result, exitIdx, exitPrice, pips) => {
    const { signalIdx, side, entry, sl } = get();
    set({
      status: "resolved",
      liveResult: result,
      exitIdx,
      exitPrice,
      pips,
      barsInTrade: exitIdx - signalIdx,
      unrealizedR: rMultiple(side, entry, sl, exitPrice),
    });
  },

  setEntryScreenshot: (blob) => set({ entryScreenshotBlob: blob }),
  setExitScreenshot: (blob) => set({ exitScreenshotBlob: blob }),
  setScreenshotPaths: (entry, exit) =>
    set({ entryScreenshotPath: entry, exitScreenshotPath: exit }),
  setLastCheckedIdx: (idx) => set({ lastCheckedIdx: idx }),

  cancel: () => set({ ...INITIAL }),
  reset: () => set({ ...INITIAL }),

  getMinCursor: () => {
    const { status, signalIdx } = get();
    return status === "active" || status === "resolved" ? signalIdx : 0;
  },
}));
