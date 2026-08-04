import type { ComboboxOption } from "@/components/ui/combobox";

/** Options share the Combobox's shape so one vocabulary feeds every control. */
export type FieldOption = ComboboxOption;

/** Maps the `X` / `X_LABELS` const pairs in lib/tradeLabels.ts to options. */
export function toOptions<T extends string>(
  values: readonly T[],
  labels: Record<T, string>,
): FieldOption[] {
  return values.map((value) => ({ value, label: labels[value] }));
}
