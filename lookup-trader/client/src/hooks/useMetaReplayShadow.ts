import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

interface MetaReplayQuery {
  symbol: string;
  timeframe: string;
  signalTs: string;
  side: 1 | -1;
}

export function useMetaReplayShadow(query: MetaReplayQuery | null) {
  return useQuery({
    queryKey: ["meta-model-replay", query],
    queryFn: () =>
      api.getMetaReplayShadow(query!.symbol, query!.timeframe, query!.signalTs, query!.side),
    enabled: !!query,
    staleTime: Infinity,
    retry: false,
  });
}
