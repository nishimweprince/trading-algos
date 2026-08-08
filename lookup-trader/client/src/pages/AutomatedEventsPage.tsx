import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ReplayChart } from "@/components/chart/ReplayChart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { formatTs } from "@/lib/format";
import type { MetaEventOutcome, MetaEventQuery, MetaEventVerdict } from "@/types";

const PAGE_SIZE = 100;

type MetaEventSortOrder =
  | "signal_ts_desc"
  | "signal_ts_asc"
  | "confidence_desc"
  | "confidence_asc";

function parseSortOrder(sortOrder: MetaEventSortOrder): {
  sort: "signal_ts" | "confidence";
  order: "asc" | "desc";
} {
  const order: "asc" | "desc" = sortOrder.endsWith("_desc") ? "desc" : "asc";
  const sort: "signal_ts" | "confidence" = sortOrder.startsWith("confidence")
    ? "confidence"
    : "signal_ts";
  return { sort, order };
}

export function AutomatedEventsPage() {
  const queryClient = useQueryClient();
  const [offset, setOffset] = useState(0);
  const [sortOrder, setSortOrder] = useState<MetaEventSortOrder>("signal_ts_desc");
  const [year, setYear] = useState("");
  const [setup, setSetup] = useState("");
  const [side, setSide] = useState("");
  const [confidence, setConfidence] = useState("");
  const [reviewStatus, setReviewStatus] = useState("");
  const [quality, setQuality] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [verdict, setVerdict] = useState<MetaEventVerdict | "">("");
  const [notes, setNotes] = useState("");
  const [postNotes, setPostNotes] = useState("");
  const [outcome, setOutcome] = useState<MetaEventOutcome | null>(null);

  const summary = useQuery({
    queryKey: ["meta-events-summary", "XAUUSD", "H1"],
    queryFn: () => api.getMetaEventSummary("XAUUSD", "H1"),
    retry: false,
  });
  const { sort, order } = parseSortOrder(sortOrder);
  const eventQuery: MetaEventQuery = {
    symbol: "XAUUSD",
    timeframe: "H1",
    offset,
    limit: PAGE_SIZE,
    sort,
    order,
    year: year ? Number(year) : undefined,
    setup: setup || undefined,
    side: side ? (Number(side) as 1 | -1) : undefined,
    confidenceMin: confidence ? Number(confidence) : undefined,
    quality: quality ? (quality as "reliable" | "unreliable") : undefined,
    reviewStatus: reviewStatus || undefined,
  };
  const events = useQuery({
    queryKey: ["meta-events", eventQuery],
    queryFn: () => api.getMetaEvents(eventQuery),
    retry: false,
  });
  const detail = useQuery({
    queryKey: ["meta-event", selectedId],
    queryFn: () => api.getMetaEvent(selectedId!),
    enabled: !!selectedId,
    retry: false,
  });

  useEffect(() => {
    setOffset(0);
  }, [year, setup, side, confidence, quality, reviewStatus, sortOrder]);
  useEffect(() => {
    setVerdict(detail.data?.verdict ?? "");
    setNotes(detail.data?.pre_notes ?? "");
    setPostNotes(detail.data?.post_notes ?? "");
    setOutcome(null);
  }, [detail.data]);

  const review = useMutation({
    mutationFn: () =>
      api.reviewMetaEvent(selectedId!, { verdict: verdict as MetaEventVerdict, notes, phase: "pre" }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["meta-event", selectedId] }),
        queryClient.invalidateQueries({ queryKey: ["meta-events"] }),
        queryClient.invalidateQueries({ queryKey: ["meta-events-summary"] }),
      ]);
    },
  });
  const reveal = useMutation({
    mutationFn: () => api.revealMetaEvent(selectedId!),
    onSuccess: async (value) => {
      setOutcome(value);
      await queryClient.invalidateQueries({ queryKey: ["meta-event", selectedId] });
    },
  });
  const savePost = useMutation({
    mutationFn: () => api.reviewMetaEvent(selectedId!, { notes: postNotes, phase: "post" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["meta-event", selectedId] }),
  });

  const chartCandles = useMemo(
    () => [...(detail.data?.history_candles ?? []), ...(outcome?.forward_candles ?? [])],
    [detail.data?.history_candles, outcome?.forward_candles],
  );
  const years = Object.keys(summary.data?.by_year ?? {}).sort();
  const setups = Object.keys(summary.data?.by_setup ?? {}).sort();

  return (
    <div className="flex h-full min-h-0 bg-zinc-950 text-zinc-100">
      <aside className="flex w-[420px] shrink-0 flex-col border-r border-zinc-800">
        <div className="space-y-3 border-b border-zinc-800 p-4">
          <div>
            <h1 className="text-lg font-semibold">Automated events</h1>
            <p className="text-xs text-zinc-500">
              {summary.data?.total ?? 0} immutable labels · reviews never change training inclusion
            </p>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <select className="h-9 rounded border border-zinc-800 bg-zinc-900 px-2 text-sm" value={year} onChange={(e) => setYear(e.target.value)}>
              <option value="">All years</option>{years.map((value) => <option key={value}>{value}</option>)}
            </select>
            <select className="h-9 rounded border border-zinc-800 bg-zinc-900 px-2 text-sm" value={side} onChange={(e) => setSide(e.target.value)}>
              <option value="">Both sides</option><option value="1">Long</option><option value="-1">Short</option>
            </select>
            <select className="h-9 rounded border border-zinc-800 bg-zinc-900 px-2 text-sm" value={setup} onChange={(e) => setSetup(e.target.value)}>
              <option value="">All setups</option>{setups.map((value) => <option key={value}>{value}</option>)}
            </select>
            <select className="h-9 rounded border border-zinc-800 bg-zinc-900 px-2 text-sm" value={reviewStatus} onChange={(e) => setReviewStatus(e.target.value)}>
              <option value="">All reviews</option><option value="unreviewed">Unreviewed</option><option value="valid">Valid</option><option value="invalid">Invalid</option><option value="uncertain">Uncertain</option><option value="revealed">Revealed</option>
            </select>
          </div>
          <Input type="number" min="0" max="1" step="0.05" placeholder="Minimum confidence" value={confidence} onChange={(e) => setConfidence(e.target.value)} />
          <select className="h-9 w-full rounded border border-zinc-800 bg-zinc-900 px-2 text-sm" value={quality} onChange={(e) => setQuality(e.target.value)}>
            <option value="">All data quality</option><option value="reliable">Reliable</option><option value="unreliable">Unreliable</option>
          </select>
          <select className="h-9 w-full rounded border border-zinc-800 bg-zinc-900 px-2 text-sm" value={sortOrder} onChange={(e) => setSortOrder(e.target.value as MetaEventSortOrder)}>
            <option value="signal_ts_desc">Newest first</option>
            <option value="signal_ts_asc">Oldest first</option>
            <option value="confidence_desc">Highest confidence</option>
            <option value="confidence_asc">Lowest confidence</option>
          </select>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {events.isLoading && <p className="p-4 text-sm text-zinc-500">Loading bounded event page…</p>}
          {events.error && <p className="p-4 text-sm text-red-400">{String(events.error)}</p>}
          {!events.isLoading && events.data?.items.length === 0 && (
            <p className="p-4 text-sm text-zinc-500">No exported events match these filters. Run the Batch 1 rebuild if the export is missing.</p>
          )}
          {events.data?.items.map((event) => (
            <button key={event.event_id} onClick={() => setSelectedId(event.event_id)} className={`w-full border-b border-zinc-900 p-3 text-left hover:bg-zinc-900 ${selectedId === event.event_id ? "bg-zinc-900" : ""}`}>
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-zinc-300">{formatTs(event.signal_ts, false)}</span>
                <Badge variant="outline">{event.side === 1 ? "Long" : "Short"}</Badge>
              </div>
              <div className="mt-1 flex items-center justify-between text-sm">
                <span>{event.primary_setup_id}</span><span className="text-zinc-500">{event.confidence.toFixed(3)}</span>
              </div>
              <p className="mt-1 text-xs text-zinc-500">{event.verdict ?? "unreviewed"}{event.revealed_at ? " · revealed" : ""}</p>
            </button>
          ))}
        </div>
        <div className="flex items-center justify-between border-t border-zinc-800 p-3 text-xs text-zinc-500">
          <Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</Button>
          <span>{events.data ? `${offset + 1}–${Math.min(offset + PAGE_SIZE, events.data.total)} / ${events.data.total}` : "—"}</span>
          <Button variant="outline" size="sm" disabled={!events.data?.has_next} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</Button>
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">
        {!selectedId && <div className="grid flex-1 place-items-center text-sm text-zinc-500">Select an event to audit its causal evidence.</div>}
        {selectedId && detail.isLoading && <div className="grid flex-1 place-items-center text-sm text-zinc-500">Loading causal detail…</div>}
        {detail.data && (
          <>
            <section className="min-h-0 flex-1">
              <ReplayChart
                candles={chartCandles}
                entry={outcome?.entry_price}
                sl={outcome?.stop_price}
                tp={outcome?.target_price}
                revealLabelOverride={outcome ? "Outcome revealed · 24 forward bars visible" : "Causal audit · future hidden"}
              />
            </section>
            <section className="grid max-h-[330px] grid-cols-2 gap-5 overflow-y-auto border-t border-zinc-800 bg-zinc-950 p-4">
              <div className="space-y-3">
                <div className="flex items-center gap-2">
                  <Badge>{detail.data.primary_setup_id}</Badge>
                  <Badge variant="outline">{detail.data.side === 1 ? "Long" : "Short"}</Badge>
                  <span className="text-xs text-zinc-500">confidence {detail.data.confidence.toFixed(3)}</span>
                </div>
                <p className="text-xs text-amber-300">Judge detector validity before revealing. This review is stored separately and cannot edit the automated label or v1 training dataset.</p>
                <div className="flex gap-2">
                  {(["valid", "invalid", "uncertain"] as MetaEventVerdict[]).map((value) => (
                    <Button key={value} size="sm" variant={verdict === value ? "operator" : "outline"} disabled={!!detail.data.revealed_at} onClick={() => setVerdict(value)}>{value}</Button>
                  ))}
                </div>
                <Textarea value={notes} disabled={!!detail.data.revealed_at} onChange={(e) => setNotes(e.target.value)} placeholder="Why does the detector look valid or invalid?" />
                <div className="flex gap-2">
                  <Button size="sm" disabled={!verdict || !!detail.data.revealed_at || review.isPending} onClick={() => review.mutate()}>Save pre-reveal review</Button>
                  <Button size="sm" variant="outline" disabled={!detail.data.verdict || reveal.isPending} onClick={() => reveal.mutate()}>Reveal outcome</Button>
                </div>
              </div>
              <div className="space-y-3">
                {!outcome && !detail.data.revealed_at && <p className="text-sm text-zinc-500">Outcome, forward candles, and cost-adjusted returns remain hidden.</p>}
                {outcome && (
                  <>
                    <div className="flex items-center gap-2"><Badge>{outcome.outcome}</Badge><span className="text-sm">gross {outcome.gross_r.toFixed(3)}R</span>{outcome.ambiguous_bar && <Badge variant="ambiguous">same-bar loss</Badge>}</div>
                    <div className="grid grid-cols-3 gap-2 text-xs">
                      {[3, 5, 8].map((pips) => <div key={pips} className="rounded border border-zinc-800 p-2"><p className="text-zinc-500">{pips}-pip net</p><p className="text-base">{outcome[`net_r_${pips}` as keyof MetaEventOutcome] as number >= 0 ? "+" : ""}{Number(outcome[`net_r_${pips}` as keyof MetaEventOutcome]).toFixed(3)}R</p></div>)}
                    </div>
                    <Textarea value={postNotes} onChange={(e) => setPostNotes(e.target.value)} placeholder="Optional post-reveal audit notes (outcome-exposed)" />
                    <Button size="sm" variant="outline" disabled={!postNotes.trim() || savePost.isPending} onClick={() => savePost.mutate()}>Save post-reveal note</Button>
                  </>
                )}
              </div>
            </section>
          </>
        )}
      </main>
    </div>
  );
}
