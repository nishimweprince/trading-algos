import { faCircleCheck, faTriangleExclamation } from "@fortawesome/free-solid-svg-icons";
import { Icon } from "@/lib/icon";
import { formatPips, formatWhen } from "@/lib/format";
import type { ExecutionStatus } from "@/lib/types";

const STATE_TONE: Record<string, string> = {
  succeeded: "text-emerald-500",
  pending: "text-amber-500",
  shadow: "text-muted-foreground",
  cancelled: "text-muted-foreground",
  rejected: "text-red-500",
  unknown: "text-amber-500",
};

function Row({ label, engine, broker }: { label: string; engine: number; broker: number }) {
  const agrees = engine === broker;
  return (
    <div className="flex items-center justify-between gap-4 border-b border-border py-2 last:border-b-0">
      <span className="text-[11px] uppercase text-muted-foreground">{label}</span>
      <div className="flex items-center gap-3 tabular-nums">
        <span className="text-xs">{engine}</span>
        <span className="text-[10px] text-muted-foreground">vs</span>
        <span className="text-xs">{broker}</span>
        <Icon
          icon={agrees ? faCircleCheck : faTriangleExclamation}
          className={`h-3 w-3 ${agrees ? "text-emerald-500" : "text-amber-500"}`}
        />
      </div>
    </div>
  );
}

export function DivergencePanel({ status }: { status: ExecutionStatus }) {
  const { divergence } = status;

  return (
    <section className="space-y-4">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium">Engine vs broker</h3>
          <p className="mt-1 text-[11px] text-muted-foreground">
            What the strategy believes it holds, next to what the account actually holds.
          </p>
        </div>
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
          <span className="border border-border px-2 py-0.5 uppercase">{status.mode}</span>
          <span className={status.gateway_ready ? "text-emerald-500" : "text-amber-500"}>
            {status.gateway_ready ? "gateway ready" : status.gateway_reason}
          </span>
        </div>
      </header>

      {status.halted_reason ? (
        <p className="border border-red-500/40 bg-red-500/5 px-3 py-2 text-xs text-red-500">
          Execution halted — {status.halted_reason}. Resting orders were cancelled; restart to
          resume.
        </p>
      ) : null}

      {!status.sends_broker_orders ? (
        <p className="border border-border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
          {status.mode === "off"
            ? "Execution is off. The engine is running as a simulation and no orders exist."
            : "Shadow mode — payloads below are exactly what would be sent, but nothing reached the broker."}
        </p>
      ) : null}

      {divergence ? (
        <div className="grid gap-6 lg:grid-cols-2">
          <div>
            <div className="mb-1 flex justify-end gap-3 text-[10px] uppercase text-muted-foreground">
              <span>engine</span>
              <span>broker</span>
              <span className="w-3" />
            </div>
            <Row
              label="Open structures"
              engine={divergence.engine_open_structures}
              broker={divergence.broker_open_positions}
            />
            <Row
              label="Resting orders"
              engine={divergence.engine_resting_orders}
              broker={divergence.broker_resting_orders}
            />
            {divergence.mean_slippage_pips !== null ? (
              <div className="flex items-center justify-between gap-4 py-2">
                <span className="text-[11px] uppercase text-muted-foreground">
                  Mean fill slippage
                </span>
                <span className="text-xs tabular-nums">
                  {formatPips(divergence.mean_slippage_pips)} over {divergence.slippage_pips.length}{" "}
                  fills
                </span>
              </div>
            ) : null}
          </div>

          <div className="space-y-2">
            {divergence.notes.map((note) => (
              <p key={note} className="text-[11px] text-muted-foreground">
                {note}
              </p>
            ))}
            {divergence.unmatched_broker_positions.length > 0 ? (
              <p className="text-[11px] text-amber-500">
                Broker positions the engine does not know about:{" "}
                {divergence.unmatched_broker_positions.join(", ")}
              </p>
            ) : null}
            {divergence.unmatched_engine_orders.length > 0 ? (
              <p className="text-[11px] text-amber-500">
                Engine orders with no broker id: {divergence.unmatched_engine_orders.join(", ")}
              </p>
            ) : null}
          </div>
        </div>
      ) : null}

      <div>
        <h4 className="mb-2 text-[11px] uppercase text-muted-foreground">
          Tracked orders ({status.tracked_orders.length})
        </h4>
        {status.tracked_orders.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No orders yet. One appears when a session stages its bracket.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[40rem] text-xs">
              <thead>
                <tr className="border-b border-border text-left text-[10px] uppercase text-muted-foreground">
                  <th className="py-2 pr-3 font-normal">Structure</th>
                  <th className="py-2 pr-3 font-normal">Side</th>
                  <th className="py-2 pr-3 font-normal">State</th>
                  <th className="py-2 pr-3 text-right font-normal">Trigger</th>
                  <th className="py-2 pr-3 text-right font-normal">Fill</th>
                  <th className="py-2 pr-3 text-right font-normal">Order</th>
                  <th className="py-2 font-normal">Submitted</th>
                </tr>
              </thead>
              <tbody>
                {status.tracked_orders.map((order) => (
                  <tr
                    key={`${order.pair_id}-${order.side}`}
                    className="border-b border-border last:border-b-0"
                  >
                    <td className="py-2 pr-3 break-all">{order.pair_id}</td>
                    <td className="py-2 pr-3">{order.side}</td>
                    <td className={`py-2 pr-3 ${STATE_TONE[order.state] ?? ""}`}>
                      {order.state}
                      {order.reason ? (
                        <span className="text-muted-foreground"> · {order.reason}</span>
                      ) : null}
                    </td>
                    <td className="py-2 pr-3 text-right tabular-nums">
                      {order.entry_price ?? "—"}
                    </td>
                    <td className="py-2 pr-3 text-right tabular-nums">{order.fill_price ?? "—"}</td>
                    <td className="py-2 pr-3 text-right tabular-nums">{order.order_id ?? "—"}</td>
                    <td className="py-2 text-muted-foreground">{formatWhen(order.submitted_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}
