/**
 * PM2 production entry — live only (devnet dry-run instance removed
 * 2026-09-16; config.devnet.yaml kept dormant for local testing).
 * pump-desk-main → config.yaml (mainnet, dashboard 8787, Telegram commands ON).
 * Restart to apply — hot-reload is NOT supported.
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
  ],
};
