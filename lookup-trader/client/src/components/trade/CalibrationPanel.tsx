import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useTrades } from "@/hooks/useTrades";
import { calibrationBuckets, calibrationRows } from "@/lib/calibration";
import { formatPercent } from "@/lib/format";
import type { Session } from "@/types";

const HELPER = "text-sm text-zinc-500";

interface CalibrationPanelProps {
  session: Session | null;
}

/**
 * Predicted vs prior vs outcome, per confidence level.
 *
 * Reads the trades already loaded for the session — the prior is copied onto the
 * occurrence at submit time precisely so this needs no second fetch and no
 * recomputation that could drift from what was on screen at the decision.
 */
export function CalibrationPanel({ session }: CalibrationPanelProps) {
  const { data: trades = [] } = useTrades(session?.session_id);

  const rows = calibrationRows(trades);
  const buckets = calibrationBuckets(rows);

  if (!session) return null;

  return (
    <Card>
      <CardHeader className="flex-row items-baseline justify-between gap-2 space-y-0">
        <CardTitle className="text-zinc-400">Calibration</CardTitle>
        <span className="tnum font-mono text-xs text-zinc-500">{rows.length}</span>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className={HELPER}>
          Your confidence against the context base rate and what actually happened. Beating the
          prior is the only evidence that reading the chart adds anything a lookup does not.
        </p>

        {rows.length === 0 ? (
          <p className={HELPER}>
            No resolved trades yet carry both a confidence and a frozen prior. Annotate a signal
            with a confidence, then mark and resolve the trade.
          </p>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="tnum w-full font-mono text-sm text-zinc-500">
                <thead>
                  <tr>
                    <th className="py-0.5 text-left font-normal">Conf</th>
                    <th className="py-0.5 text-right font-normal">You</th>
                    <th className="py-0.5 text-right font-normal">Base</th>
                    <th className="py-0.5 text-right font-normal">Actual</th>
                    <th className="py-0.5 text-right font-normal">n</th>
                  </tr>
                </thead>
                <tbody>
                  {buckets.map((b) => (
                    <tr key={b.confidence}>
                      <td className="py-0.5">{b.confidence}/5</td>
                      <td className="py-0.5 text-right">{formatPercent(b.predicted)}</td>
                      <td className="py-0.5 text-right">{formatPercent(b.prior)}</td>
                      <td className="py-0.5 text-right text-zinc-300">{formatPercent(b.actual)}</td>
                      <td className="py-0.5 text-right">{b.n}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className={HELPER}>
              {rows.length} decided trade{rows.length === 1 ? "" : "s"} — far too few to read yet.
              The buckets are here so the record accumulates from the start.
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
