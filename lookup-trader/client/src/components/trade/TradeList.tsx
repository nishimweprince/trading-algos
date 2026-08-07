import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ResultBadge } from "@/components/trade/ResultBadge";
import { useTrades } from "@/hooks/useTrades";
import { api } from "@/lib/api";
import { priorCell } from "@/lib/calibration";
import { formatPercent, formatPrice, formatTs } from "@/lib/format";
import { formatTradingSession, type TradingSession } from "@/lib/tradingSession";
import { SKIP_REASON_LABELS, SKIP_REASONS } from "@/lib/tradeLabels";
import { cn } from "@/lib/utils";
import type { RGridEntry, Session } from "@/types";

type SkipReason = (typeof SKIP_REASONS)[number];

interface TradeListProps {
  session: Session | null;
  blinded?: boolean;
}

/** Largest target multiple that would still have won against the same stop. */
function bestGridTarget(grid?: Record<string, RGridEntry> | null): string | null {
  if (!grid) return null;
  const wins = Object.entries(grid)
    .filter(([, entry]) => entry.result === "win")
    .map(([target]) => Number(target));
  return wins.length > 0 ? String(Math.max(...wins)) : null;
}

export function TradeList({ session, blinded }: TradeListProps) {
  const { data: trades = [], isLoading } = useTrades(session?.session_id);

  return (
    <Card>
      <CardHeader className="flex-row items-baseline justify-between gap-2 space-y-0">
        <CardTitle className="text-zinc-400">Session trades</CardTitle>
        <span className="tnum text-xs text-zinc-500">{trades.length}</span>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-zinc-500">Loading…</p>}
        {!isLoading && trades.length === 0 && (
          <p className="text-sm text-zinc-500">
            {session ? "Mark a trade to start the record." : "Start a session to label trades."}
          </p>
        )}
        <div className="space-y-2">
          {trades.map((t) => {
            const thumbPath = t.screenshot_entry ?? t.screenshot_exit;
            const thumbUrl = thumbPath ? api.screenshotUrl(thumbPath) : null;
            const sessionLabel =
              t.session && ["asian", "london", "ny", "off_hours"].includes(t.session)
                ? formatTradingSession(t.session as TradingSession)
                : t.session;

            const skipped = t.outcome_kind === "skipped";
            const prior = priorCell(t);

            return (
              <div
                key={t.id}
                className={cn(
                  "rounded border border-zinc-800 p-2 text-xs",
                  (skipped || t.excluded) && "opacity-60",
                )}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium">{t.setup_id}</span>
                      {skipped ? (
                        <span className="rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] uppercase text-zinc-500">
                          skipped
                        </span>
                      ) : (
                        <ResultBadge result={t.result} source={t.source} />
                      )}
                    </div>
                    <div className="tnum mt-1 text-zinc-400">
                      {formatTs(t.ts, blinded)} · E {formatPrice(t.entry)} · R{" "}
                      {t.realized_r?.toFixed(2) ?? "—"}
                      {t.pips_captured != null && (
                        <>
                          {" "}
                          · {t.pips_captured >= 0 ? "+" : ""}
                          {t.pips_captured.toFixed(1)} pips
                        </>
                      )}
                    </div>
                    {/* How far it ran either way — a loss that reached +2R is a
                        different lesson from one that never went green. */}
                    {(t.mfe_r != null || t.mae_r != null) && (
                      <div className="tnum mt-0.5 text-zinc-500">
                        peak {t.mfe_r?.toFixed(2) ?? "—"}R · dip {t.mae_r?.toFixed(2) ?? "—"}R
                        {bestGridTarget(t.r_grid) && <> · best {bestGridTarget(t.r_grid)}R target</>}
                      </div>
                    )}
                    {/* What you thought, what the context did historically, and
                        what happened — the three numbers calibration is made of. */}
                    {prior?.win_rate != null && (
                      <div className="tnum mt-0.5 text-zinc-500">
                        {t.confidence != null && <>predicted {t.confidence}/5 · </>}
                        base {formatPercent(prior.win_rate)} · {t.result ?? "open"}
                      </div>
                    )}
                    <p className="mt-0.5 flex flex-wrap gap-x-2 text-zinc-500">
                      {sessionLabel && <span>{sessionLabel} session</span>}
                      {skipped && t.skip_reason && (
                        <span>{SKIP_REASON_LABELS[t.skip_reason as SkipReason] ?? t.skip_reason}</span>
                      )}
                      {t.peeked && <span className="text-amber-500/80">saw ahead</span>}
                      {t.assisted && <span className="text-amber-500/80">saw base rate</span>}
                      {t.ambiguous_bar && <span className="text-amber-500/80">ambiguous bar</span>}
                      {t.entry_feasible === false && (
                        <span className="text-amber-500/80">entry never traded</span>
                      )}
                      {t.context_reliable === false && (
                        <span className="text-amber-500/80">thin history</span>
                      )}
                    </p>
                  </div>
                  {thumbUrl && (
                    <a href={thumbUrl} target="_blank" rel="noreferrer" className="shrink-0">
                      <img
                        src={thumbUrl}
                        alt="Trade chart"
                        className="h-12 w-20 rounded border border-zinc-800 object-cover"
                      />
                    </a>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
