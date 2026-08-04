import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { BarSeriesQuery } from "@/types";

/**
 * Per-bar features for the chart overlays.
 *
 * Keyed on `revealedThrough` so advancing the cursor refetches and progressively
 * unlocks the forward half of older bars. The gating itself is the server's job;
 * this hook only tells it where the cursor is.
 */
export function useBarFeatureSeries(query: BarSeriesQuery | null) {
  return useQuery({
    queryKey: ["bar-features", query],
    queryFn: () => api.getBarSeries(query!),
    enabled: !!query?.symbol && !!query.timeframe && !!query.dateFrom && !!query.dateTo,
    staleTime: Infinity,
    retry: false,
  });
}
