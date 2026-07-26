/** PM2 production entry — runs the bot only (embedded dashboard on config.dashboard.port). */
module.exports = {
  apps: [
    {
      name: 'pump-desk',
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
    },
  ],
};
