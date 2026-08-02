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
      source?: string;
      min_samples?: number;
    }) => api.compare(body),
  });
}
