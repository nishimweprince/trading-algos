import { Fragment, useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, ChevronDown, ChevronRight } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { formatTs } from "@/lib/format";
import type { MetaShadowEvent, MetaShadowPrediction } from "@/types";

const SYMBOL = "XAUUSD";
const TIMEFRAME = "H1";
const PAGE_SIZE = 50;
const CONFIDENCE_BUCKETS = [0.5, 0.6, 0.7, 0.8, 0.9, 1.001];

interface Bucket {
  label: string;
  n: number;
  win: number;
  loss: number;
  timeout: number;
  netRSum: number;
  netRCount: number;
}

function bucketize<K extends string>(events: MetaShadowEvent[], keyOf: (e: MetaShadowEvent) => K) {
  const byKey = new Map<K, Bucket>();
  for (const event of events) {
    if (event.outcome == null) continue;
    const key = keyOf(event);
    const bucket = byKey.get(key) ?? { label: key, n: 0, win: 0, loss: 0, timeout: 0, netRSum: 0, netRCount: 0 };
    bucket.n += 1;
    if (event.outcome === "win") bucket.win += 1;
    else if (event.outcome === "loss") bucket.loss += 1;
    else if (event.outcome === "timeout") bucket.timeout += 1;
    if (event.net_r_5 != null) {
      bucket.netRSum += event.net_r_5;
      bucket.netRCount += 1;
    }
    byKey.set(key, bucket);
  }
  return [...byKey.values()];
}

function confidenceBucketLabel(confidence: number): string {
  for (let i = 0; i < CONFIDENCE_BUCKETS.length - 1; i++) {
    const lo = CONFIDENCE_BUCKETS[i];
    const hi = CONFIDENCE_BUCKETS[i + 1];
    if (confidence >= lo && confidence < hi) return `${lo.toFixed(1)}-${Math.min(hi, 1).toFixed(1)}`;
  }
  const lastLo = CONFIDENCE_BUCKETS[CONFIDENCE_BUCKETS.length - 2];
  return `${lastLo.toFixed(1)}-1.0`;
}

function winPct(bucket: Bucket): string {
  return bucket.n ? `${((100 * bucket.win) / bucket.n).toFixed(1)}%` : "—";
}

function avgNetR(bucket: Bucket): string {
  if (!bucket.netRCount) return "—";
  const avg = bucket.netRSum / bucket.netRCount;
  return `${avg >= 0 ? "+" : ""}${avg.toFixed(3)}R`;
}

function SideBadge({ side }: { side: 1 | -1 }) {
  const isLong = side === 1;
  return (
    <Badge variant={isLong ? "long" : "short"} className="gap-1">
      {isLong ? <ArrowUp className="size-3" aria-hidden /> : <ArrowDown className="size-3" aria-hidden />}
      {isLong ? "Long" : "Short"}
    </Badge>
  );
}

function fmtNum(value: number | null | undefined, digits = 3): string {
  if (value == null) return "—";
  return value.toFixed(digits);
}

function fmtSignedR(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(3)}`;
}

function fmtBool(value: boolean | null | undefined): string {
  if (value == null) return "—";
  return value ? "yes" : "no";
}

/** The event's prediction from whichever artifact is live-active right now, not whatever was active when it was scored. */
function activePrediction(
  event: MetaShadowEvent,
  activeVersion: string | null | undefined,
): MetaShadowPrediction | null {
  if (!activeVersion) return null;
  return event.predictions.find((p) => p.artifact_version === activeVersion) ?? null;
}

function predictionRole(
  prediction: MetaShadowPrediction,
  activeVersion: string | null | undefined,
  challengerVersion: string | null | undefined,
): string {
  if (prediction.artifact_version === activeVersion) return "Active";
  if (prediction.artifact_version === challengerVersion) return "Challenger";
  return "—";
}

function RecommendationBadge({ prediction }: { prediction: MetaShadowPrediction | null }) {
  if (!prediction) return <span className="text-zinc-500">—</span>;
  return (
    <Badge variant={prediction.would_take ? "win" : "outline"}>
      {prediction.would_take ? "Take" : "Skip"}
    </Badge>
  );
}

function DetailField({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] uppercase tracking-wide text-zinc-500">{label}</dt>
      <dd className="tnum mt-0.5 break-all text-zinc-200">{value}</dd>
    </div>
  );
}

function SignalDetail({
  event,
  activeVersion,
  challengerVersion,
}: {
  event: MetaShadowEvent;
  activeVersion: string | null | undefined;
  challengerVersion: string | null | undefined;
}) {
  const recommended = activePrediction(event, activeVersion);
  return (
    <div className="space-y-4 border-t border-zinc-800 bg-zinc-950/80 px-3 py-4">
      <section>
        <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-zinc-500">Signal</h4>
        <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <DetailField label="Event ID" value={event.event_id} />
          <DetailField label="Signal ts" value={formatTs(event.signal_ts)} />
          <DetailField label="Side" value={<SideBadge side={event.side} />} />
          <DetailField
            label="Recommendation"
            value={
              <span className="flex items-center gap-2">
                <RecommendationBadge prediction={recommended} />
                {recommended && (
                  <span className="text-zinc-500">
                    {recommended.probability.toFixed(3)} vs {recommended.threshold.toFixed(3)} threshold
                  </span>
                )}
              </span>
            }
          />
          <DetailField label="Primary setup" value={event.primary_setup_id} />
          <DetailField label="Setup IDs" value={event.setup_ids.join(", ") || "—"} />
          <DetailField label="Confidence" value={event.confidence.toFixed(3)} />
          <DetailField label="Signal close" value={fmtNum(event.signal_close, 2)} />
          <DetailField label="ATR at signal" value={fmtNum(event.atr_at_signal, 4)} />
        </dl>
      </section>

      <section>
        <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-zinc-500">Trade</h4>
        <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <DetailField label="Entry ts" value={event.entry_ts ? formatTs(event.entry_ts) : "—"} />
          <DetailField label="Entry price" value={fmtNum(event.entry_price, 2)} />
          <DetailField label="Stop" value={fmtNum(event.stop_price, 2)} />
          <DetailField label="Target" value={fmtNum(event.target_price, 2)} />
          <DetailField label="Exit ts" value={event.exit_ts ? formatTs(event.exit_ts) : "—"} />
          <DetailField label="Exit price" value={fmtNum(event.exit_price, 2)} />
          <DetailField label="Bars to resolution" value={event.bars_to_resolution ?? "—"} />
          <DetailField label="Ambiguous bar" value={fmtBool(event.ambiguous_bar)} />
          <DetailField label="Observed spread" value={fmtNum(event.observed_spread, 4)} />
        </dl>
      </section>

      <section>
        <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-zinc-500">Outcome</h4>
        <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <DetailField
            label="Outcome"
            value={
              event.outcome ? <Badge variant={event.outcome}>{event.outcome}</Badge> : "—"
            }
          />
          <DetailField label="Gross R" value={fmtSignedR(event.gross_r)} />
          <DetailField label="Net R (3)" value={fmtSignedR(event.net_r_3)} />
          <DetailField label="Net R (5)" value={fmtSignedR(event.net_r_5)} />
          <DetailField label="Net R (8)" value={fmtSignedR(event.net_r_8)} />
        </dl>
      </section>

      <section>
        <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-zinc-500">Excursions</h4>
        <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <DetailField label="MFE (R)" value={<span className="text-zinc-500">not tracked</span>} />
          <DetailField label="MAE (R)" value={<span className="text-zinc-500">not tracked</span>} />
        </dl>
        <p className="mt-2 text-[11px] text-zinc-500">
          Excursions are only computed for manually-labelled replay trades, not live meta-shadow signals.
        </p>
      </section>

      <section>
        <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-zinc-500">
          Eligibility & delivery
        </h4>
        <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <DetailField label="State" value={event.state} />
          <DetailField label="Forward eligible" value={fmtBool(event.forward_evaluation_eligible)} />
          <DetailField label="Ineligible reason" value={event.ineligible_reason ?? "—"} />
          <DetailField label="Calendar coverage OK" value={fmtBool(event.calendar_coverage_ok)} />
          <DetailField label="Calendar manifest" value={event.calendar_manifest_sha256 ?? "—"} />
          <DetailField label="Notification" value={event.notification_status ?? "pending"} />
          <DetailField label="Notify attempts" value={event.notification_attempts ?? "—"} />
          <DetailField label="Notified at" value={event.notified_at ? formatTs(event.notified_at) : "—"} />
        </dl>
      </section>

      <section>
        <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-zinc-500">Predictions</h4>
        {event.predictions.length === 0 ? (
          <p className="text-xs text-zinc-500">No shadow predictions stored.</p>
        ) : (
          <table className="w-full table-fixed text-left text-xs">
            <colgroup>
              <col className="w-[38%]" />
              <col className="w-[14%]" />
              <col className="w-[10%]" />
              <col className="w-[12%]" />
              <col className="w-[12%]" />
              <col className="w-[14%]" />
            </colgroup>
            <thead className="text-zinc-500">
              <tr>
                <th className="truncate pb-1.5 pr-3 font-normal">Artifact</th>
                <th className="truncate pb-1.5 pr-3 font-normal">Role</th>
                <th className="pb-1.5 pr-3 font-normal text-right">Features</th>
                <th className="pb-1.5 pr-3 font-normal text-right">Prob</th>
                <th className="pb-1.5 pr-3 font-normal text-right">Threshold</th>
                <th className="pb-1.5 font-normal">Recommendation</th>
              </tr>
            </thead>
            <tbody className="text-zinc-300">
              {event.predictions.map((pred) => (
                <tr key={`${pred.artifact_version}-${pred.meta_feature_version}`} className="border-t border-zinc-800">
                  <td className="truncate py-1 pr-3" title={pred.artifact_version}>
                    {pred.artifact_version}
                  </td>
                  <td className="truncate py-1 pr-3 text-zinc-500">
                    {predictionRole(pred, activeVersion, challengerVersion)}
                  </td>
                  <td className="tnum py-1 pr-3 text-right">v{pred.meta_feature_version}</td>
                  <td className="tnum py-1 pr-3 text-right">{pred.probability.toFixed(3)}</td>
                  <td className="tnum py-1 pr-3 text-right">{pred.threshold.toFixed(3)}</td>
                  <td className="py-1">
                    <RecommendationBadge prediction={pred} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function BucketTable({ title, buckets }: { title: string; buckets: Bucket[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <table className="w-full text-left text-xs">
          <thead className="text-zinc-500">
            <tr>
              <th className="pb-2 font-normal">Group</th>
              <th className="pb-2 font-normal text-right">n</th>
              <th className="pb-2 font-normal text-right">Win</th>
              <th className="pb-2 font-normal text-right">Loss</th>
              <th className="pb-2 font-normal text-right">T/O</th>
              <th className="pb-2 font-normal text-right">Win%</th>
              <th className="pb-2 font-normal text-right">Avg net R (5)</th>
            </tr>
          </thead>
          <tbody className="text-zinc-300">
            {buckets.map((bucket) => (
              <tr key={bucket.label} className="border-t border-zinc-800">
                <td className="py-1.5">
                  {bucket.label === "Long" || bucket.label === "Short" ? (
                    <SideBadge side={bucket.label === "Long" ? 1 : -1} />
                  ) : (
                    bucket.label
                  )}
                </td>
                <td className="py-1.5 text-right">{bucket.n}</td>
                <td className="py-1.5 text-right">{bucket.win}</td>
                <td className="py-1.5 text-right">{bucket.loss}</td>
                <td className="py-1.5 text-right">{bucket.timeout}</td>
                <td className="py-1.5 text-right">{winPct(bucket)}</td>
                <td className="py-1.5 text-right">{avgNetR(bucket)}</td>
              </tr>
            ))}
            {buckets.length === 0 && (
              <tr>
                <td colSpan={7} className="py-3 text-center text-zinc-500">
                  No resolved signals yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

export function LiveSignalsPage() {
  const [offset, setOffset] = useState(0);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const status = useQuery({
    queryKey: ["meta-model-status"],
    queryFn: () => api.getMetaModelStatus(),
    retry: false,
  });
  const summary = useQuery({
    queryKey: ["meta-shadow-history", SYMBOL, TIMEFRAME, "forward", "summary"],
    queryFn: () => api.getMetaShadowHistory(SYMBOL, TIMEFRAME, 0, 200, undefined, true),
    retry: false,
  });
  const page = useQuery({
    queryKey: ["meta-shadow-history", SYMBOL, TIMEFRAME, "forward", offset],
    queryFn: () => api.getMetaShadowHistory(SYMBOL, TIMEFRAME, offset, PAGE_SIZE, undefined, true),
    retry: false,
  });

  const events = useMemo(() => summary.data?.items ?? [], [summary.data]);
  const pageEvents = useMemo(() => page.data?.items ?? [], [page.data]);
  const total = page.data?.total ?? summary.data?.total ?? 0;
  const hasNext = offset + pageEvents.length < total;

  const resolved = useMemo(() => events.filter((e) => e.outcome != null), [events]);
  const open = useMemo(() => events.filter((e) => e.state === "open" || e.state === "awaiting_entry"), [events]);

  const confidenceBuckets = useMemo(() => {
    const buckets = bucketize(events, (event) => confidenceBucketLabel(event.confidence));
    return buckets.sort((a, b) => a.label.localeCompare(b.label));
  }, [events]);

  const sideBuckets = useMemo(
    () => bucketize(events, (event) => (event.side === 1 ? "Long" : "Short")).sort((a, b) => b.n - a.n),
    [events],
  );

  const setupBuckets = useMemo(
    () => bucketize(events, (event) => event.primary_setup_id).sort((a, b) => b.n - a.n),
    [events],
  );

  const winRate = resolved.length ? (100 * resolved.filter((e) => e.outcome === "win").length) / resolved.length : null;
  const netRValues = resolved.map((e) => e.net_r_5).filter((v): v is number => v != null);
  const avgNetR5 = netRValues.length ? netRValues.reduce((a, b) => a + b, 0) / netRValues.length : null;

  const activeVersion = status.data?.active_shadow?.active_version ?? null;
  const challengerVersion = status.data?.active_shadow?.challenger_version ?? null;

  const forwardStart = status.data?.ledger.forward_shadow_start_ts;
  const daysElapsed = forwardStart ? Math.max((Date.now() - new Date(forwardStart).getTime()) / 86_400_000, 1 / 24) : null;
  const perDay = daysElapsed ? events.length / daysElapsed : null;

  const csvHref = `/api/export/meta-shadow?symbol=${SYMBOL}&timeframe=${TIMEFRAME}&forward_only=true`;

  function setPage(nextOffset: number) {
    setOffset(nextOffset);
    setExpandedId(null);
  }

  function toggleExpanded(eventId: string) {
    setExpandedId((current) => (current === eventId ? null : eventId));
  }

  function onRowKeyDown(event: KeyboardEvent<HTMLTableRowElement>, eventId: string) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      toggleExpanded(eventId);
    }
  }

  return (
    <div className="h-full min-h-0 overflow-y-auto bg-zinc-950 p-6 text-zinc-100">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Live signals</h1>
          <p className="text-xs text-zinc-500">
            Forward-generated meta-events since {forwardStart ? formatTs(forwardStart) : "—"} · research shadow, orders disabled
          </p>
        </div>
        <a
          href={csvHref}
          className="rounded border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
        >
          Download CSV
        </a>
      </div>

      {summary.isLoading && <p className="text-sm text-zinc-500">Loading live signals…</p>}
      {summary.error && <p className="text-sm text-red-400">{String(summary.error)}</p>}

      {!summary.isLoading && (
        <>
          <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-5">
            <Card>
              <CardHeader className="pb-1">
                <CardTitle className="text-zinc-500">Live signals</CardTitle>
              </CardHeader>
              <CardContent className="pt-0 text-2xl font-semibold">{events.length}</CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-1">
                <CardTitle className="text-zinc-500">Avg / day</CardTitle>
              </CardHeader>
              <CardContent className="pt-0 text-2xl font-semibold">{perDay != null ? perDay.toFixed(1) : "—"}</CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-1">
                <CardTitle className="text-zinc-500">Win rate</CardTitle>
              </CardHeader>
              <CardContent className="pt-0 text-2xl font-semibold">{winRate != null ? `${winRate.toFixed(1)}%` : "—"}</CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-1">
                <CardTitle className="text-zinc-500">Avg net R (5)</CardTitle>
              </CardHeader>
              <CardContent className="pt-0 text-2xl font-semibold">{avgNetR5 != null ? `${avgNetR5 >= 0 ? "+" : ""}${avgNetR5.toFixed(3)}` : "—"}</CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-1">
                <CardTitle className="text-zinc-500">Open</CardTitle>
              </CardHeader>
              <CardContent className="pt-0 text-2xl font-semibold">{open.length}</CardContent>
            </Card>
          </div>

          <div className="mb-6 grid grid-cols-1 gap-4 xl:grid-cols-3">
            <BucketTable title="By confidence" buckets={confidenceBuckets} />
            <BucketTable title="By side" buckets={sideBuckets} />
            <BucketTable title="By setup" buckets={setupBuckets} />
          </div>

          <Card>
            <CardHeader className="flex flex-row items-center justify-between space-y-0">
              <CardTitle>Recent signals</CardTitle>
              <div className="flex items-center gap-2 text-xs text-zinc-500">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset === 0 || page.isFetching}
                  onClick={() => setPage(Math.max(0, offset - PAGE_SIZE))}
                >
                  Previous
                </Button>
                <span>
                  {total
                    ? `${offset + 1}–${Math.min(offset + pageEvents.length, total)} / ${total}`
                    : "—"}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!hasNext || page.isFetching}
                  onClick={() => setPage(offset + PAGE_SIZE)}
                >
                  Next
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              {page.isLoading && <p className="py-3 text-center text-xs text-zinc-500">Loading page…</p>}
              {page.error && <p className="py-3 text-center text-xs text-red-400">{String(page.error)}</p>}
              {!page.isLoading && !page.error && (
                <table className="w-full text-left text-xs">
                  <thead className="text-zinc-500">
                    <tr>
                      <th className="w-6 pb-2 font-normal" aria-hidden />
                      <th className="pb-2 pr-4 font-normal">Signal</th>
                      <th className="pb-2 pr-4 font-normal">Side</th>
                      <th className="pb-2 pr-4 font-normal">Setup</th>
                      <th className="pb-2 pr-8 font-normal text-right">Confidence</th>
                      <th className="pb-2 pr-4 font-normal">Recommendation</th>
                      <th className="pb-2 pr-4 font-normal">State</th>
                      <th className="pb-2 pr-4 font-normal">Outcome</th>
                      <th className="pb-2 pr-8 font-normal text-right">Net R (5)</th>
                      <th className="pb-2 font-normal">Notified</th>
                    </tr>
                  </thead>
                  <tbody className="text-zinc-300">
                    {pageEvents.map((event) => {
                      const expanded = expandedId === event.event_id;
                      return (
                        <Fragment key={event.event_id}>
                          <tr
                            className="cursor-pointer border-t border-zinc-800 hover:bg-zinc-900"
                            tabIndex={0}
                            aria-expanded={expanded}
                            onClick={() => toggleExpanded(event.event_id)}
                            onKeyDown={(e) => onRowKeyDown(e, event.event_id)}
                          >
                            <td className="py-1.5 text-zinc-500">
                              {expanded ? (
                                <ChevronDown className="size-3.5" aria-hidden />
                              ) : (
                                <ChevronRight className="size-3.5" aria-hidden />
                              )}
                            </td>
                            <td className="py-1.5 pr-4 whitespace-nowrap">{formatTs(event.signal_ts)}</td>
                            <td className="py-1.5 pr-4">
                              <SideBadge side={event.side} />
                            </td>
                            <td className="py-1.5 pr-4">{event.primary_setup_id}</td>
                            <td className="tnum py-1.5 pr-8 text-right">{event.confidence.toFixed(3)}</td>
                            <td className="py-1.5 pr-4">
                              <RecommendationBadge prediction={activePrediction(event, activeVersion)} />
                            </td>
                            <td className="py-1.5 pr-4 text-zinc-500">{event.state}</td>
                            <td className="py-1.5 pr-4">
                              {event.outcome ? (
                                <Badge variant={event.outcome}>{event.outcome}</Badge>
                              ) : (
                                <span className="text-zinc-500">—</span>
                              )}
                            </td>
                            <td className="tnum py-1.5 pr-8 text-right">{fmtSignedR(event.net_r_5)}</td>
                            <td className="py-1.5 text-zinc-500">{event.notification_status ?? "pending"}</td>
                          </tr>
                          {expanded && (
                            <tr className="border-t border-zinc-800">
                              <td colSpan={10} className="p-0">
                                <SignalDetail
                                  event={event}
                                  activeVersion={activeVersion}
                                  challengerVersion={challengerVersion}
                                />
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      );
                    })}
                    {pageEvents.length === 0 && (
                      <tr>
                        <td colSpan={10} className="py-3 text-center text-zinc-500">
                          No forward-generated signals yet.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
