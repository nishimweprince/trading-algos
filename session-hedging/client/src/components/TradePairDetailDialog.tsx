import { useEffect, useRef } from "react";
import { faXmark } from "@fortawesome/free-solid-svg-icons";
import { Button } from "@/components/ui/button";
import {
  backtestCsvSections,
  buildBacktestCsvRow,
  csvColumnLabel,
  formatCsvDetailValue,
  type BacktestCsvContext,
} from "@/lib/csv";
import { Icon } from "@/lib/icon";
import { SESSION_LABEL, type TradePairResult } from "@/lib/types";

interface TradePairDetailDialogProps {
  open: boolean;
  pair: TradePairResult | null;
  context: BacktestCsvContext | null;
  onClose: () => void;
}

export function TradePairDetailDialog({
  open,
  pair,
  context,
  onClose,
}: TradePairDetailDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && pair && context) {
      if (!dialog.open) dialog.showModal();
      return;
    }
    if (dialog.open) dialog.close();
  }, [open, pair, context]);

  if (!pair || !context) return null;

  const row = buildBacktestCsvRow(context, pair);
  const title = `${SESSION_LABEL[pair.session] ?? pair.session} · ${formatCsvDetailValue("entry_time", row.entry_time)}`;

  return (
    <dialog
      ref={dialogRef}
      className="fixed inset-0 z-50 m-auto w-[min(100vw-2rem,42rem)] max-h-[min(85vh,720px)] overflow-hidden border border-border bg-background p-0 text-foreground shadow-none backdrop:bg-black/70 open:flex open:flex-col"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClose={onClose}
    >
      <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
        <div>
          <p className="text-[11px] uppercase text-muted-foreground">Pair details</p>
          <h2 className="mt-1 text-sm font-medium">{title}</h2>
          <p className="mt-1 text-[11px] text-muted-foreground">{row.pair_id}</p>
        </div>
        <Button type="button" variant="ghost" size="icon" onClick={onClose} aria-label="Close">
          <Icon icon={faXmark} className="h-3.5 w-3.5" />
        </Button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        <div className="space-y-6">
          {context.entry_mode === "hedge_pair" && pair.survivor_side ? (
            <section>
              <h3 className="mb-2 text-[11px] uppercase text-muted-foreground">
                Survivor path
              </h3>
              <dl className="grid gap-x-4 gap-y-2 sm:grid-cols-2">
                <Detail label="Side" value={pair.survivor_side} />
                <Detail label="First stop" value={pair.first_stop_ts ?? "—"} />
                <Detail label="Post-failure MFE" value={rValue(pair.survivor_post_failure_mfe_r)} />
                <Detail label="Post-failure MAE" value={rValue(pair.survivor_post_failure_mae_r)} />
                <Detail label="Peak giveback" value={rValue(pair.survivor_peak_giveback_r)} />
                <Detail label="Ratchet advances" value={String(pair.survivor_ratchet_advances ?? 0)} />
                <Detail label="Ratchet armed" value={pair.survivor_ratchet_armed_ts ?? "—"} />
                <Detail
                  label="Exit efficiency"
                  value={
                    pair.survivor_exit_efficiency == null
                      ? "—"
                      : `${(pair.survivor_exit_efficiency * 100).toFixed(1)}%`
                  }
                />
              </dl>
            </section>
          ) : null}
          {backtestCsvSections(context.entry_mode).map((section) => (
            <section key={section.title}>
              <h3 className="mb-2 text-[11px] uppercase text-muted-foreground">{section.title}</h3>
              <dl className="grid gap-x-4 gap-y-2 sm:grid-cols-2">
                {section.columns.map((column) => (
                  <div key={column} className="min-w-0">
                    <dt className="text-[10px] uppercase text-muted-foreground">
                      {csvColumnLabel(column)}
                    </dt>
                    <dd className="mt-0.5 break-all text-xs">{formatCsvDetailValue(column, row[column])}</dd>
                  </div>
                ))}
              </dl>
            </section>
          ))}
        </div>
      </div>
    </dialog>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] uppercase text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 break-all text-xs">{value}</dd>
    </div>
  );
}

function rValue(value: number | null | undefined): string {
  return value == null ? "—" : `${value.toFixed(2)}R`;
}
