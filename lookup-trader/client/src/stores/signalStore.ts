import { create } from "zustand";
import type { CompareResult, Signal } from "@/types";

export interface SignalAnnotations {
  setup_id?: string;
  side?: 1 | -1;
  confluence: string[];
  calendar_flag: boolean;
  calendar_tags: string;
  confidence: number;
  at_key_level: boolean;
  level_type: string;
  consolidation_before: boolean;
}

interface SignalState {
  activeSignalId: string | null;
  lastSignal: Signal | null;
  annotations: SignalAnnotations;
  setActiveSignalId: (id: string | null) => void;
  setLastSignal: (signal: Signal | null) => void;
  setAnnotations: (patch: Partial<SignalAnnotations>) => void;
  reset: () => void;
}

const DEFAULT_ANNOTATIONS: SignalAnnotations = {
  confluence: [],
  calendar_flag: false,
  calendar_tags: "",
  confidence: 3,
  at_key_level: false,
  level_type: "none",
  consolidation_before: false,
};

export const useSignalStore = create<SignalState>((set) => ({
  activeSignalId: null,
  lastSignal: null,
  annotations: { ...DEFAULT_ANNOTATIONS },
  setActiveSignalId: (id) => set({ activeSignalId: id }),
  setLastSignal: (signal) => set({ lastSignal: signal }),
  setAnnotations: (patch) =>
    set((s) => ({ annotations: { ...s.annotations, ...patch } })),
  reset: () =>
    set({
      activeSignalId: null,
      lastSignal: null,
      annotations: { ...DEFAULT_ANNOTATIONS },
    }),
}));

export function compareFromSignal(signal: Signal | null): CompareResult | null {
  if (!signal?.compare_at_signal) return null;
  return signal.compare_at_signal as CompareResult;
}
