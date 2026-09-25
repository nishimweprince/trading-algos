/**
 * PumpSwap canonical-pool fee schedule (SOL-quoted), keyed by market cap.
 *
 * Source: https://pump.fun/docs/fees, verified 2026-09-25. The on-chain
 * `FeeConfig.feeTiers` (pump-fees program) is authoritative and is preferred
 * by FeeModel when it has been fetched; this table is the offline fallback and
 * the basis for re-costing historical exports that carry no pool state.
 *
 * Work plan 2026-09-25 F4: paper accounting charged a flat 0.25 %/leg while
 * the real schedule is 1.25 %/leg below 420 SOL mcap — every graduation.
 */

export interface FeeTierBps {
  /** Lower bound of the tier, SOL market cap (inclusive). */
  mcapSol: number;
  creatorBps: number;
  protocolBps: number;
  lpBps: number;
}

export const PUMPSWAP_FEE_TIERS: readonly FeeTierBps[] = [
  { mcapSol: 0, creatorBps: 30, protocolBps: 93, lpBps: 2 },
  { mcapSol: 420, creatorBps: 95, protocolBps: 5, lpBps: 20 },
  { mcapSol: 1_470, creatorBps: 90, protocolBps: 5, lpBps: 20 },
  { mcapSol: 2_460, creatorBps: 85, protocolBps: 5, lpBps: 20 },
  { mcapSol: 3_440, creatorBps: 80, protocolBps: 5, lpBps: 20 },
  { mcapSol: 4_420, creatorBps: 75, protocolBps: 5, lpBps: 20 },
  { mcapSol: 9_820, creatorBps: 70, protocolBps: 5, lpBps: 20 },
  { mcapSol: 14_740, creatorBps: 65, protocolBps: 5, lpBps: 20 },
  { mcapSol: 19_650, creatorBps: 60, protocolBps: 5, lpBps: 20 },
  { mcapSol: 24_560, creatorBps: 55, protocolBps: 5, lpBps: 20 },
  { mcapSol: 29_470, creatorBps: 50, protocolBps: 5, lpBps: 20 },
  { mcapSol: 34_380, creatorBps: 45, protocolBps: 5, lpBps: 20 },
  { mcapSol: 39_300, creatorBps: 40, protocolBps: 5, lpBps: 20 },
  { mcapSol: 44_210, creatorBps: 35, protocolBps: 5, lpBps: 20 },
  { mcapSol: 49_120, creatorBps: 30, protocolBps: 5, lpBps: 20 },
  { mcapSol: 54_030, creatorBps: 27.5, protocolBps: 5, lpBps: 20 },
  { mcapSol: 58_940, creatorBps: 25, protocolBps: 5, lpBps: 20 },
  { mcapSol: 63_860, creatorBps: 22.5, protocolBps: 5, lpBps: 20 },
  { mcapSol: 68_770, creatorBps: 20, protocolBps: 5, lpBps: 20 },
  { mcapSol: 73_681, creatorBps: 17.5, protocolBps: 5, lpBps: 20 },
  { mcapSol: 78_590, creatorBps: 15, protocolBps: 5, lpBps: 20 },
  { mcapSol: 83_500, creatorBps: 12.5, protocolBps: 5, lpBps: 20 },
  { mcapSol: 88_400, creatorBps: 10, protocolBps: 5, lpBps: 20 },
  { mcapSol: 93_330, creatorBps: 7.5, protocolBps: 5, lpBps: 20 },
  { mcapSol: 98_240, creatorBps: 5, protocolBps: 5, lpBps: 20 },
];

/** Total pump token supply in whole tokens (1e9 at 6 dp) — the mcap basis for pump mints. */
export const PUMP_TOTAL_SUPPLY_WHOLE = 1_000_000_000;

export function totalBps(t: FeeTierBps): number {
  return t.creatorBps + t.protocolBps + t.lpBps;
}

/**
 * Tier for a market cap. Mirrors the SDK's `calculateFeeTier`: below the first
 * threshold -> first tier; otherwise the highest tier whose threshold <= mcap.
 * Unknown / non-finite mcap -> first tier (every fresh graduation is there,
 * and it is the most expensive, so the fallback is pessimistic).
 */
export function tierForMcap(mcapSol: number | null | undefined, tiers: readonly FeeTierBps[] = PUMPSWAP_FEE_TIERS): FeeTierBps {
  const first = tiers[0]!;
  if (mcapSol === null || mcapSol === undefined || !Number.isFinite(mcapSol)) return first;
  for (let i = tiers.length - 1; i >= 0; i--) {
    if (mcapSol >= tiers[i]!.mcapSol) return tiers[i]!;
  }
  return first;
}

export function feeBpsForMcap(mcapSol: number | null | undefined, tiers?: readonly FeeTierBps[]): number {
  return totalBps(tierForMcap(mcapSol, tiers));
}

/** Market cap (SOL) from a price in SOL per whole token. */
export function mcapFromPrice(priceSolPerToken: number, supplyWhole = PUMP_TOTAL_SUPPLY_WHOLE): number | null {
  return priceSolPerToken > 0 && Number.isFinite(priceSolPerToken) ? priceSolPerToken * supplyWhole : null;
}
