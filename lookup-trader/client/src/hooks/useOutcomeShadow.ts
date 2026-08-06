import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

interface OutcomeShadowQuery {
  symbol: string;
  timeframe: string;
  signalTs: string;
}

export function useOutcomeShadow(query: OutcomeShadowQuery | null) {
  return useQuery({
    queryKey: ["outcome-model-shadow", query],
    queryFn: () => api.getOutcomeShadow(query!.symbol, query!.timeframe, query!.signalTs),
    enabled: !!query?.symbol && !!query.timeframe && !!query.signalTs,
    staleTime: Infinity,
    retry: false,
    placeholderData: undefined,
  });
}
