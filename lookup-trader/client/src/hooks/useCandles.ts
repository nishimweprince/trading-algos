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

export function useCandleBounds(symbol: string, timeframe: string) {
  return useQuery({
    queryKey: ["candleBounds", symbol, timeframe],
    queryFn: () => api.getCandleBounds(symbol, timeframe),
    enabled: !!symbol && !!timeframe,
    staleTime: 60_000,
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

export function useCandlePage(
  symbol: string,
  timeframe: string,
  dateFrom: string,
  dateTo: string,
  enabled: boolean,
  offset = 0,
) {
  return useQuery({
    queryKey: ["candle-page", symbol, timeframe, dateFrom, dateTo, offset],
    queryFn: () => api.getCandlePage(symbol, timeframe, dateFrom, dateTo, offset),
    enabled: enabled && !!symbol && !!timeframe && !!dateFrom && !!dateTo,
    staleTime: 60_000,
    gcTime: 60_000,
  });
}
