/**
 * PM2 — Frankfurt (feat/fra-lean). Three processes on one VM:
 *
 *   pump-desk-trader     detection, fast screen, execution, exits, guards.
 *                        Pinned to its own core(s); nothing else runs there.
 *   pump-desk-research   enrichment, cluster warm-up, shadow, decision shadow
 *                        (src/research-worker.ts), research Helius key.
 *   pump-desk-dashboard  read-only dashboard on 127.0.0.1:8787, published only
 *                        through the Cloudflare Tunnel.
 *
 * Core split from the VM's core count: 4+ → trader 0,1 / rest 2..n;
 * 2-3 → trader 1 / rest 0; 1 → no pinning.
 *
 * All three share data/scalper.db (WAL). Restart to apply config changes.
 */
const path = require('node:path');

const os = require('node:os');

const cwd = path.resolve(__dirname, '..', '..');
const node = process.execPath;
const cores = os.cpus().length;
const traderCores = cores >= 4 ? '0,1' : cores >= 2 ? String(cores - 1) : null;
const otherCores = cores >= 4 ? `2-${cores - 1}` : cores >= 2 ? '0' : null;
/** `taskset -c <cores> node …` when pinning applies, else plain node. */
const pinned = (coreList, nodeArgs) =>
  coreList ? { script: 'taskset', args: ['-c', coreList, node, ...nodeArgs] } : { script: node, args: nodeArgs };
const common = {
  cwd,
  interpreter: 'none',
  instances: 1,
  exec_mode: 'fork',
  autorestart: true,
  max_restarts: 5,
  min_uptime: '30s',
  restart_delay: 10_000,
  env_file: '.env',
};

module.exports = {
  apps: [
    {
      ...common,
      name: 'pump-desk-trader',
      ...pinned(traderCores, ['--experimental-strip-types', '--max-semi-space-size=64', 'src/index.ts']),
      env: { NODE_ENV: 'production', CONFIG_PATH: 'config.yaml', PUMPDESK_ROLE: 'trader' },
      kill_timeout: 15_000, // let open exits finish their shutdown path
    },
    {
      ...common,
      name: 'pump-desk-research',
      ...pinned(otherCores, ['--experimental-strip-types', 'src/research-worker.ts']),
      env: { NODE_ENV: 'production', CONFIG_PATH: 'config.yaml', PUMPDESK_ROLE: 'research' },
    },
    {
      ...common,
      name: 'pump-desk-dashboard',
      ...pinned(otherCores, ['--experimental-strip-types', 'src/dashboard/standalone.ts']),
      env: { NODE_ENV: 'production', CONFIG_PATH: 'config.yaml' },
    },
  ],
};
