/**
 * Sequential wallet equity (paper-honesty Unit 6): what the wallet would
 * actually have done, one position at a time, instead of the per-horizon
 * sums that assume infinite capital.
 *
 * Each position locks size + ATA rent at entry and releases the size at the
 * exit; the rent follows at the sweeper cadence (Unit 5 timing — never, when
 * sweeping is off). A position is skipped when the free balance cannot cover
 * size + rent, or when maxConcurrent positions are already open. Skips are
 * counted, never silently absorbed: a strategy that cannot afford its own
 * signals is worse than its per-trade mean.
 *
 * Equity counts locked rent at full value (it is still ours, recoverable by
 * a future sweep) — peakLockedSol shows how much is tied up. Pnl inputs are
 * net at each track's own size (the wallet-sized legs), so the simulator
 * prices no legs itself; garbage in is the caller's problem.
 */
export interface EquityPosition {
  mint: string;
  entryAtMs: number;
  /** Must be >= entryAtMs (the summary adapter filters the rest). */
  exitAtMs: number;
  /** Track size in SOL; must be > 0. */
  sizeSol: number;
  /** Net pnl in SOL at this size (already cost-adjusted). */
  pnlSol: number;
}

export interface EquityOpts {
  /** Wallet balance when the first position opens; must be >= 0. */
  startBalanceSol: number;
  /** Max simultaneous open positions; <= 0 = unlimited. */
  maxConcurrent?: number | undefined;
  /** ATA rent locked per position. */
  rentSol: number;
  /** Rent release delay after the exit; <= 0 = never. */
  sweepDelayMs: number;
}

export interface EquityPoint {
  mint: string;
  /** ISO exit time. */
  at: string;
  pnlSol: number;
  /** Total equity (free + locked) just after this exit. */
  equitySol: number;
}

export interface EquityResult {
  startBalanceSol: number;
  /** One point per taken exit, in exit-time order. */
  curve: EquityPoint[];
  finalEquitySol: number;
  peakEquitySol: number;
  maxDrawdownSol: number;
  maxDrawdownPct: number | null;
  taken: number;
  /** Skipped: free balance could not cover size + rent. */
  skippedCapital: number;
  /** Skipped: maxConcurrent positions already open. */
  skippedConcurrent: number;
  peakLockedSol: number;
}

type Event =
  | { at: number; kind: 0; pos: number } // rent release
  | { at: number; kind: 1; pos: number } // exit
  | { at: number; kind: 2; pos: number }; // entry

export function simulateEquity(positions: readonly EquityPosition[], opts: EquityOpts): EquityResult {
  const maxConcurrent = opts.maxConcurrent ?? 0;
  const events: Event[] = positions.flatMap((p, i) => {
    const ev: Event[] = [
      { at: p.entryAtMs, kind: 2, pos: i },
      { at: p.exitAtMs, kind: 1, pos: i },
    ];
    if (opts.sweepDelayMs > 0) ev.push({ at: p.exitAtMs + opts.sweepDelayMs, kind: 0, pos: i });
    return ev;
  });
  // Releases before exits before entries at the same instant: freed capital
  // is immediately available, and a position exiting now does not block one
  // entering now.
  events.sort((a, b) => a.at - b.at || a.kind - b.kind || a.pos - b.pos);

  const open = new Set<number>();
  const exited = new Set<number>();
  let free = opts.startBalanceSol;
  let locked = 0;
  let peakLockedSol = 0;
  let peak = opts.startBalanceSol;
  let maxDrawdownSol = 0;
  let maxDrawdownPct: number | null = null;
  let taken = 0;
  let skippedCapital = 0;
  let skippedConcurrent = 0;
  const curve: EquityPoint[] = [];

  for (const e of events) {
    const p = positions[e.pos]!;
    if (e.kind === 2) {
      if (open.size >= maxConcurrent && maxConcurrent > 0) {
        skippedConcurrent++;
        continue;
      }
      const need = p.sizeSol + opts.rentSol;
      if (free < need) {
        skippedCapital++;
        continue;
      }
      free -= need;
      locked += need;
      peakLockedSol = Math.max(peakLockedSol, locked);
      open.add(e.pos);
    } else if (e.kind === 1) {
      if (!open.delete(e.pos)) continue;
      taken++;
      exited.add(e.pos);
      locked -= p.sizeSol;
      free += p.sizeSol + p.pnlSol;
      const equity = free + locked;
      peak = Math.max(peak, equity);
      const dd = peak - equity;
      if (dd > maxDrawdownSol) {
        maxDrawdownSol = dd;
        maxDrawdownPct = peak > 0 ? (dd / peak) * 100 : null;
      }
      curve.push({ mint: p.mint, at: new Date(e.at).toISOString(), pnlSol: p.pnlSol, equitySol: equity });
    } else {
      // A skipped position's release event is a no-op.
      if (!exited.has(e.pos)) continue;
      locked -= opts.rentSol;
      free += opts.rentSol;
    }
  }

  return {
    startBalanceSol: opts.startBalanceSol,
    curve,
    finalEquitySol: free + locked,
    peakEquitySol: peak,
    maxDrawdownSol,
    maxDrawdownPct,
    taken,
    skippedCapital,
    skippedConcurrent,
    peakLockedSol,
  };
}
