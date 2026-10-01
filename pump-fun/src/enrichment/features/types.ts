/**
 * Manipulation & population features (work plan 2026-09-25 P3.3), grounded in
 * the 15.2 M-coin pump.fun study [R4]: creator clusters control most launches,
 * wash trading inflates graduation, copycats graduate at a tenth of the rate
 * of originals, and coordinated atomic dumps are common.
 *
 * Every field is optional: a feature that is disabled, over budget or
 * unresolvable is simply absent (never a guess). Persisted in
 * candidates/positions.features_json; consumed by H13 and the learned filter.
 */
export interface ManipulationFeatures {
  /** Creator funding cluster (1–2 hops). */
  cluster?: {
    funder: string | null;
    root: string;
    /** Launches by any wallet of the cluster in the last 7 days (incl. this one when recorded). */
    launches7d: number;
    wallets: number;
    /** Funder is a high-traffic hub (exchange / router): not used to cluster. */
    hubFunder: boolean;
  };
  /** Bonding-curve history (creation-slot bundle, wash ratio). */
  curve?: {
    /** Creation slot when the scan reached the curve's first transaction, else null. */
    creationSlot: number | null;
    txScanned: number;
    /**
     * Oldest slot among the signatures scanned. When `creationSlot` is null
     * the curve is at least this old — a lower bound on mint age (H12).
     * Absent on rows recorded before 2026-09-28.
     */
    oldestSlotScanned?: number | null;
    /** The feature budget cut the scan short; parse-derived fields are null. */
    partial?: boolean;
    /**
     * True while the wash / bundle parse runs after the verdict (screening
     * pages only); false once it has been patched in. Absent on rows parsed
     * inline (before 2026-09-28, or with H13 wash / bundle thresholds set).
     */
    deferred?: boolean;
    /** % of supply bought inside the creation slot (dev buy + same-slot bundle). */
    bundleSharePct: number | null;
    creationSlotBuyers: number | null;
    /** Share of sampled curve volume from wallets that both bought and sold. */
    washRatio: number | null;
  };
  /** Creation -> migration, ms (exact sources of `mintAgeAtMigration` only; null for a lower bound). */
  timeToGraduateMs?: number | null;
  copycat?: { nameMatches: number; imageMatches: number; isCopycat: boolean };
  snipers?: { earlyBuyers: number; knownSnipers: number; sniperBuyShare: number };
  holderQuality?: { sampled: number; freshWallets: number; freshRatio: number };
  /** Feature keys that errored or ran out of budget. */
  missing?: string[];
}
