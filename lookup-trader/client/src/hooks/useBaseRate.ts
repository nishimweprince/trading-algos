import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { BaseRateQuery } from "@/types";

/**
 * Follows the cursor rather than waiting for a button, because the base rate is
 * a property of the bar, not of a comparison the operator chose to run. A 503
 * means no feature store has been built yet, which is a setup step and not
 * something retrying will fix.
 */
export function useBaseRate(query: BaseRateQuery | null) {
  return useQuery({
    queryKey: ["base-rate", query],
    queryFn: () => api.getBaseRate(query!),
    enabled: !!query?.symbol && !!query.timeframe && !!query.signalTs,
    staleTime: Infinity,
    retry: false,
  });
}
