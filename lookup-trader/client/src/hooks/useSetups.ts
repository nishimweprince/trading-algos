import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useSetups() {
  return useQuery({ queryKey: ["setups"], queryFn: api.getSetups, staleTime: 5 * 60 * 1000 });
}
