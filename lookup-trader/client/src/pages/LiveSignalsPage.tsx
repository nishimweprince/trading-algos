import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { formatTs } from "@/lib/format";
import type { MetaShadowEvent } from "@/types";

const SYMBOL = "XAUUSD";
const TIMEFRAME = "H1";
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
                <td className="py-1.5">{bucket.label}</td>
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
  const status = useQuery({
    queryKey: ["meta-model-status"],
    queryFn: () => api.getMetaModelStatus(),
    retry: false,
  });
  const history = useQuery({
    queryKey: ["meta-shadow-history", SYMBOL, TIMEFRAME, "forward"],
    queryFn: () => api.getMetaShadowHistory(SYMBOL, TIMEFRAME, 0, 200, undefined, true),
    retry: false,
  });

  const events = useMemo(() => history.data?.items ?? [], [history.data]);
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

  const forwardStart = status.data?.ledger.forward_shadow_start_ts;
  const daysElapsed = forwardStart ? Math.max((Date.now() - new Date(forwardStart).getTime()) / 86_400_000, 1 / 24) : null;
  const perDay = daysElapsed ? events.length / daysElapsed : null;

  const csvHref = `/api/export/meta-shadow?symbol=${SYMBOL}&timeframe=${TIMEFRAME}&forward_only=true`;

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

      {history.isLoading && <p className="text-sm text-zinc-500">Loading live signals…</p>}
      {history.error && <p className="text-sm text-red-400">{String(history.error)}</p>}

      {!history.isLoading && (
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
            <CardHeader>
              <CardTitle>Recent signals</CardTitle>
            </CardHeader>
            <CardContent>
              <table className="w-full text-left text-xs">
                <thead className="text-zinc-500">
                  <tr>
                    <th className="pb-2 font-normal">Signal</th>
                    <th className="pb-2 font-normal">Side</th>
                    <th className="pb-2 font-normal">Setup</th>
                    <th className="pb-2 font-normal text-right">Confidence</th>
                    <th className="pb-2 font-normal">State</th>
                    <th className="pb-2 font-normal">Outcome</th>
                    <th className="pb-2 font-normal text-right">Net R (5)</th>
                    <th className="pb-2 font-normal">Notified</th>
                  </tr>
                </thead>
                <tbody className="text-zinc-300">
                  {events.map((event) => (
                    <tr key={event.event_id} className="border-t border-zinc-800">
                      <td className="py-1.5">{formatTs(event.signal_ts)}</td>
                      <td className="py-1.5">
                        <Badge variant="outline">{event.side === 1 ? "Long" : "Short"}</Badge>
                      </td>
                      <td className="py-1.5">{event.primary_setup_id}</td>
                      <td className="py-1.5 text-right">{event.confidence.toFixed(3)}</td>
                      <td className="py-1.5 text-zinc-500">{event.state}</td>
                      <td className="py-1.5">
                        {event.outcome ? <Badge variant={event.outcome}>{event.outcome}</Badge> : <span className="text-zinc-500">—</span>}
                      </td>
                      <td className="py-1.5 text-right">
                        {event.net_r_5 != null ? `${event.net_r_5 >= 0 ? "+" : ""}${event.net_r_5.toFixed(3)}` : "—"}
                      </td>
                      <td className="py-1.5 text-zinc-500">{event.notification_status ?? "pending"}</td>
                    </tr>
                  ))}
                  {events.length === 0 && (
                    <tr>
                      <td colSpan={8} className="py-3 text-center text-zinc-500">
                        No forward-generated signals yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
