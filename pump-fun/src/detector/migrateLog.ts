/**
 * Shared migration-log matcher for the on-chain feeds (Helius WS, LaserStream).
 *
 * pump.fun has emitted `Instruction: Migrate` and now emits
 * `Instruction: MigrateV2` (all recent graduations); `(?:V\d+)?` covers both
 * and any future revision. The match stays anchored to end-of-line because the
 * program also logs an unrelated `Instruction: MigrateBondingCurveCreator`
 * (creator fee-sharing config migration, replayable on old/already-graduated
 * mints) that an unanchored match would misread as a fresh graduation.
 */
export const MIGRATE_LOG = /Instruction:\s*Migrate(?:V\d+)?\s*$/im;

export function hasMigrateLog(logs: readonly string[]): boolean {
  return logs.some((l) => MIGRATE_LOG.test(l));
}
