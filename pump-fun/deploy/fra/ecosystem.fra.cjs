/**
 * PM2 — Frankfurt (feat/fra-lean). Three processes on one 4-vCPU VM:
 *
 *   pump-desk-trader     detection, fast screen, execution, exits, guards.
 *                        Pinned to cores 0-1; nothing else runs there.
 *   pump-desk-research   enrichment, cluster warm-up, shadow, decision shadow
 *                        (src/research-worker.ts), research Helius key. Cores 2-3.
 *   pump-desk-dashboard  read-only dashboard on 127.0.0.1:8787, published only
 *                        through the Cloudflare Tunnel. Cores 2-3.
 *
 * All three share data/scalper.db (WAL). Restart to apply config changes.
 */
const path = require('node:path');

const cwd = path.resolve(__dirname, '..', '..');
const node = process.execPath;
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
      script: 'taskset',
      args: ['-c', '0,1', node, '--experimental-strip-types', '--max-semi-space-size=64', 'src/index.ts'],
      env: { NODE_ENV: 'production', CONFIG_PATH: 'config.yaml', PUMPDESK_ROLE: 'trader' },
      kill_timeout: 15_000, // let open exits finish their shutdown path
    },
    {
      ...common,
      name: 'pump-desk-research',
      script: 'taskset',
      args: ['-c', '2,3', node, '--experimental-strip-types', 'src/research-worker.ts'],
      env: { NODE_ENV: 'production', CONFIG_PATH: 'config.yaml', PUMPDESK_ROLE: 'research' },
    },
    {
      ...common,
      name: 'pump-desk-dashboard',
      script: 'taskset',
      args: ['-c', '2,3', node, '--experimental-strip-types', 'src/dashboard/standalone.ts'],
      env: { NODE_ENV: 'production', CONFIG_PATH: 'config.yaml' },
    },
  ],
};
