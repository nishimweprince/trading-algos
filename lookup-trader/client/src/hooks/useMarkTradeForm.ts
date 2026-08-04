/**
 * The trade-marking form, owned by ReplayPage rather than TradeForm.
 *
 * It lives at the page because three things outside the sidebar need it: the
 * chart writes levels into it when the operator picks a price, the chart reads
 * them back to draw the lines, and ComparePanel derives the planned R:R from
 * them. Keeping it in TradeForm meant mirroring the values into page state and
 * reconciling both directions by hand — and the sidebar's tabs unmount
 * TradeForm, so the form itself could not be the source of truth from there.
 */
import { useForm, type UseFormReturn } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";

/** Price fields arrive from the inputs as strings; the API takes numbers. */
const price = (label: string) =>
  z
    .string()
    .min(1, `${label} is required`)
    .refine((v) => Number(v) > 0, `${label} must be a price above zero`)
    .transform(Number);

export const markTradeSchema = z.object({
  setup_id: z.string().min(1, "Pick a setup"),
  side: z.union([z.literal(1), z.literal(-1)]),
  entry: price("Entry"),
  sl: price("Stop"),
  tp: price("Target"),
  notes: z.string().optional(),
  calendar_flag: z.boolean().optional(),
  calendar_tags: z.string().optional(),
});

export type MarkTradeValues = z.input<typeof markTradeSchema>;
export type MarkTradeParsed = z.output<typeof markTradeSchema>;
export type MarkTradeForm = UseFormReturn<MarkTradeValues, unknown, MarkTradeParsed>;

export const EMPTY_MARK_TRADE: MarkTradeValues = {
  setup_id: "",
  side: 1,
  entry: "",
  sl: "",
  tp: "",
  notes: "",
  calendar_flag: false,
  calendar_tags: "",
};

export function useMarkTradeForm(): MarkTradeForm {
  return useForm<MarkTradeValues, unknown, MarkTradeParsed>({
    resolver: zodResolver(markTradeSchema),
    defaultValues: EMPTY_MARK_TRADE,
  });
}

/** Levels come back as strings; the chart and payloads want numbers or null. */
export function toLevel(raw: unknown): number | null {
  if (raw === "" || raw == null) return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}
