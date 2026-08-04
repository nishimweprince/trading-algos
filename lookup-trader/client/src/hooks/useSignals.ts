import { useMutation } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { SignalSubmit } from "@/types";

export function useSubmitSignal() {
  return useMutation({
    mutationFn: (body: SignalSubmit) => api.submitSignal(body),
  });
}
