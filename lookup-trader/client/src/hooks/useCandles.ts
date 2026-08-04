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

/** Context features at a bar, so the operator does not have to guess at them.
 *  The endpoint reads no bars past the signal, so this cannot leak the future. */
export function useSignalContext(symbol: string, timeframe: string, signalTs: string | null) {
  return useQuery({
    queryKey: ["context", symbol, timeframe, signalTs],
    queryFn: () => api.getContext(symbol, timeframe, signalTs!),
    enabled: !!symbol && !!timeframe && !!signalTs,
    staleTime: Infinity,
    retry: false,
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
