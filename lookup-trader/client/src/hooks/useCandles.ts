import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useSymbols() {
  return useQuery({ queryKey: ["symbols"], queryFn: api.getSymbols });
}

export function useTimeframes(symbol: string) {
  return useQuery({
    queryKey: ["timeframes", symbol],
    queryFn: () => api.getTimeframes(symbol),
    enabled: !!symbol,
  });
}

export function useCandles(
  symbol: string,
  timeframe: string,
  dateFrom: string,
  dateTo: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["candles", symbol, timeframe, dateFrom, dateTo],
    queryFn: () => api.getCandles(symbol, timeframe, dateFrom, dateTo),
    enabled: enabled && !!symbol && !!timeframe && !!dateFrom && !!dateTo,
    staleTime: Infinity,
  });
}
