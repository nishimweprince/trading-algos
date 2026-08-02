import { useMutation } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { SessionCreate } from "@/types";

export function useCreateSession() {
  return useMutation({
    mutationFn: (body: SessionCreate) => api.createSession(body),
  });
}
