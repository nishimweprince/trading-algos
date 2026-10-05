/**
 * npm run racer:calibration -- [--forward-hours N] [--min-n N]
 *
 * Paper-twin calibration report (racer/calibration.ts): entry-time fill
 * predictions vs forward-realized land rates per bucket, cost regime drift
 * vs the configured assumption, and drift alerts. Prints JSON.
 */
import { loadConfig } from '../config/load.ts';
import { openDb } from '../persistence/db.ts';
import { getRacerCalibration } from '../dashboard/queries.ts';

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

async function main(): Promise<void> {
  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  const db = openDb({ path: config.persistence.dbPath });
  const forwardHours = Number(arg('--forward-hours'));
  const minN = Number(arg('--min-n'));
  const report = getRacerCalibration(db, {
    ...(Number.isFinite(forwardHours) && forwardHours > 0 ? { forwardHours } : {}),
    ...(Number.isFinite(minN) && minN > 0 ? { minN: Math.floor(minN) } : {}),
    sellCostSol: config.racer.liveSellCostSol,
    liveCostSol: config.racer.liveCostSol,
  });
  console.log(JSON.stringify(report, null, 2));
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
