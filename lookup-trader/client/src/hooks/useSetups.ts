import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ComboboxOption } from "@/components/ui/combobox";

export function useSetups() {
  return useQuery({ queryKey: ["setups"], queryFn: api.getSetups, staleTime: 5 * 60 * 1000 });
}

/** Headings for the setup picker, keyed by the server's `category`. */
const CATEGORY_LABELS: Record<string, string> = {
  chart_pattern: "Chart patterns",
  fibonacci: "Fibonacci patterns",
  key_level: "Key levels",
  candlestick: "Candlestick",
};

/**
 * Setups as combobox options, shared by every picker so the grouping and the
 * search terms stay identical wherever a setup is chosen.
 */
export function useSetupOptions(): ComboboxOption[] {
  const { data: setups = [] } = useSetups();
  return useMemo(
    () =>
      setups.map((s) => ({
        value: s.setup_id,
        label: s.name,
        group: s.category ? (CATEGORY_LABELS[s.category] ?? s.category) : undefined,
        // The id is searchable too — "inv_head" finds the inverse pattern.
        keywords: [s.setup_id, s.category ?? ""],
      })),
    [setups],
  );
}
