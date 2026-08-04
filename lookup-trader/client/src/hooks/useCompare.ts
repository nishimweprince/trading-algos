import { useMutation } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { CompareContext } from "@/types";

export function useCompare() {
  return useMutation({
    mutationFn: (body: {
      setup_id: string;
      symbol: string;
      timeframe: string;
      context: CompareContext;
      pinned?: string[];
      source?: string;
      min_samples?: number;
      exclude_peeked?: boolean;
      exclude_assisted?: boolean;
      blinded_only?: boolean;
      break_even_win_rate?: number;
    }) => api.compare(body),
  });
}
