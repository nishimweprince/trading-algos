import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { TradeSubmit } from "@/types";

export function useTrades(sessionId?: string | null) {
  return useQuery({
    queryKey: ["trades", sessionId],
    queryFn: () => api.getTrades(sessionId ?? undefined),
    enabled: !!sessionId,
  });
}

export function useSubmitTrade() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: TradeSubmit) => api.submitTrade(body),
    onSuccess: (_data, variables) => {
      qc.invalidateQueries({ queryKey: ["trades", variables.session_id] });
    },
  });
}
