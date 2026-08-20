import { ENTRY_MODE_LABEL, type EntryModeComparisonReport } from "@/lib/types";

interface Props {
  report: EntryModeComparisonReport;
}

function number(value: number | null, digits = 2) {
  return value == null ? "—" : value.toFixed(digits);
}

export function EntryModeComparison({ report }: Props) {
  const delta = report.hedge_vs_synthetic;
  return (
    <section className="border-b border-border px-5 py-6 md:px-10">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="text-sm font-medium">Four-mode comparison</h2>
          <p className="mt-1 text-[11px] text-muted-foreground">
            {report.bar_count.toLocaleString()} identical bars · fingerprint {report.candle_set_sha256.slice(0, 12)}
          </p>
        </div>
        <p className="text-[11px] text-muted-foreground">
          Hedge − synthetic net: {number(delta.net_difference_pips)} pips · {number(delta.net_difference_r)}R
        </p>
      </div>
      <div className="mt-4 overflow-x-auto border border-border">
        <table className="w-full min-w-[1180px] text-left text-[11px]">
          <thead className="border-b border-border bg-muted/30 uppercase text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-normal">Mode</th>
              <th className="px-3 py-2 font-normal">Gross / net pips</th>
              <th className="px-3 py-2 font-normal">Gross / net R</th>
              <th className="px-3 py-2 font-normal">Expectancy pips / R</th>
              <th className="px-3 py-2 font-normal">Profit factor</th>
              <th className="px-3 py-2 font-normal">Win excl. BE</th>
              <th className="px-3 py-2 font-normal">TP / required</th>
              <th className="px-3 py-2 font-normal">Exec. / finance cost</th>
              <th className="px-3 py-2 font-normal">DD pips / R</th>
              <th className="px-3 py-2 font-normal">BE cost / side</th>
              <th className="px-3 py-2 font-normal">Sides / entry / exit</th>
              <th className="px-3 py-2 font-normal">Hold med / p95</th>
              <th className="px-3 py-2 font-normal">Max conc. / suppressed / open</th>
              <th className="px-3 py-2 font-normal">PropGuard</th>
            </tr>
          </thead>
          <tbody>
            {report.rows.map((row) => (
              <tr key={row.entry_mode} className="border-b border-border last:border-0">
                <td className="px-3 py-2.5 font-medium">{ENTRY_MODE_LABEL[row.entry_mode]}</td>
                <td className="px-3 py-2.5 tabular-nums">{number(row.gross_pips)} / {number(row.net_pips)}</td>
                <td className="px-3 py-2.5 tabular-nums">{number(row.gross_r)} / {number(row.net_r)}</td>
                <td className="px-3 py-2.5 tabular-nums">{number(row.gross_expectancy_pips)} / {number(row.net_expectancy_pips)}<br />{number(row.gross_expectancy_r)}R / {number(row.net_expectancy_r)}R</td>
                <td className="px-3 py-2.5 tabular-nums">{number(row.gross_profit_factor)} / {number(row.net_profit_factor)}</td>
                <td className="px-3 py-2.5 tabular-nums">{number(row.gross_win_rate_excl_be == null ? null : row.gross_win_rate_excl_be * 100, 1)}% / {number(row.net_win_rate_excl_be == null ? null : row.net_win_rate_excl_be * 100, 1)}%</td>
                <td className="px-3 py-2.5 tabular-nums">{number(row.survivor_tp_rate == null ? null : row.survivor_tp_rate * 100, 1)}% / {number(row.breakeven_tp_rate_required == null ? null : row.breakeven_tp_rate_required * 100, 1)}%</td>
                <td className="px-3 py-2.5 tabular-nums">{number(row.execution_cost_pips)} / {number(row.financing_cost_pips)}</td>
                <td className="px-3 py-2.5 tabular-nums">{number(row.gross_max_drawdown_pips)} / {number(row.net_max_drawdown_pips)}<br />{number(row.gross_max_drawdown_r)}R / {number(row.net_max_drawdown_r)}R</td>
                <td className="px-3 py-2.5 tabular-nums">{number(row.breakeven_pips_per_completed_side)}</td>
                <td className="px-3 py-2.5 tabular-nums">{row.transaction_sides} / {row.entry_fill_sides} / {row.exit_fill_sides}<br /><span className="text-muted-foreground">{number(row.cost_side_equivalents)} weighted</span></td>
                <td className="px-3 py-2.5 tabular-nums">{number(row.median_hold_hours, 1)}h / {number(row.p95_hold_hours, 1)}h</td>
                <td className="px-3 py-2.5 tabular-nums">{row.max_concurrent_structures} / {row.suppressed_signals} / {row.unresolved_structures}</td>
                <td className="px-3 py-2.5">{row.prop_guard_breached ? row.prop_guard_breach_reason ?? "breached" : "clear"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-4 border-y border-border py-3">
        <p className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
          Hedge − synthetic reconciliation · pips
        </p>
        <div className="mt-2 flex flex-wrap items-end gap-x-3 gap-y-2 font-mono text-xs tabular-nums">
          <LedgerTerm label="payoff" value={number(delta.gross_payoff_effect_pips)} />
          <span className="pb-0.5 text-muted-foreground">+</span>
          <LedgerTerm label="gap" value={number(delta.gap_effect_pips)} />
          <span className="pb-0.5 text-muted-foreground">+</span>
          <LedgerTerm label="same-bar" value={number(delta.same_bar_effect_pips)} />
          <span className="pb-0.5 text-muted-foreground">−</span>
          <LedgerTerm label="cost" value={number(delta.total_cost_difference_pips)} />
          <span className="pb-0.5 text-muted-foreground">=</span>
          <LedgerTerm label="net" value={number(delta.net_difference_pips)} />
          <span className="pb-0.5 text-[10px] text-muted-foreground">
            error {number(delta.reconciliation_error_pips, 8)}
          </span>
        </div>
      </div>
    </section>
  );
}

function LedgerTerm({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex flex-col">
      <span className="font-sans text-[9px] uppercase tracking-wider text-muted-foreground">{label}</span>
      <span>{value}</span>
    </span>
  );
}
