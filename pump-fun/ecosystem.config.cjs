/**
 * PM2 production entries — one app per profile. Each instance reads the same
 * `.env` file (env_file) but a different config, DB, and dashboard port:
 *
 *   pump-desk-main → config.yaml        (mainnet, dashboard 8787, Telegram commands ON)
 *   pump-desk      → config.devnet.yaml (devnet,  dashboard 8788, Telegram commands OFF)
 *
 * Only pump-desk-main may poll Telegram getUpdates: both instances share one
 * bot token and two pollers 409-conflict (enforced via alerts.commandsEnabled
 * in the yaml, not here). Restart to apply — hot-reload is NOT supported.
 */
const base = {
  cwd: __dirname,
  script: 'src/index.ts',
  interpreter: 'node',
  interpreter_args: '--experimental-strip-types',
  instances: 1,
  exec_mode: 'fork',
  autorestart: true,
  max_restarts: 5,
  min_uptime: '30s',
  restart_delay: 10_000,
  env: {
    NODE_ENV: 'production',
  },
  env_file: '.env',
};

module.exports = {
  apps: [
    {
      ...base,
      name: 'pump-desk-main',
      env: {
        ...base.env,
        CONFIG_PATH: 'config.yaml',
      },
    },
    {
      ...base,
      name: 'pump-desk',
      env: {
        ...base.env,
        CONFIG_PATH: 'config.devnet.yaml',
      },
    },
  ],
};
