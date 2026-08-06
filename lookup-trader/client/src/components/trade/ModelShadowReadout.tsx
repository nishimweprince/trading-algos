import { formatShadowDirection } from "@/lib/modelShadow";
import type { OutcomeShadow } from "@/types";

export function ModelShadowReadout({
  result,
  error,
  loading,
}: {
  result: OutcomeShadow | null;
  error: Error | null;
  loading: boolean;
}) {
  return (
    <div
      className="border border-dashed border-white/20 bg-white/[0.025] px-2 py-1.5"
      aria-label="Model shadow"
    >
      <div className="flex items-baseline justify-between gap-2">
        <p className="text-[11px] font-medium tracking-[0.14em] text-white/65 uppercase">
          Model shadow
        </p>
        <span className="text-[10px] text-amber-300/70 uppercase">pilot · unpromoted</span>
      </div>
      {loading && <p className="mt-1 text-xs text-white/45">Loading closed-bar inference…</p>}
      {error && <p className="mt-1 text-xs text-white/45">Unavailable — {error.message}</p>}
      {result && (
        <>
          <div className="mt-1 grid gap-x-3 gap-y-0.5 font-mono text-xs text-white/70 sm:grid-cols-2">
            <span>{formatShadowDirection(result.long)}</span>
            <span>{formatShadowDirection(result.short)}</span>
          </div>
          <p className="mt-1 text-[10px] leading-tight text-white/35">
            Calibrated pilot probabilities only. Not used by recommendation logic. Artifact{" "}
            {result.artifact_version}.
          </p>
        </>
      )}
    </div>
  );
}
