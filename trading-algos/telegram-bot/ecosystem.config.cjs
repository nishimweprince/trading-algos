module.exports = {
  apps: [
    {
      name: 'telegram-signal-bot',
      script: 'dist/index.js',
      cwd: __dirname,
      instances: 1,
      exec_mode: 'fork',
      autorestart: true,
      max_restarts: 10,
      min_uptime: '60s',
      env: { NODE_ENV: 'production' },
      error_file: 'logs/pm2.err.log',
      out_file: 'logs/pm2.out.log',
      merge_logs: true,
      time: true,
    },
  ],
};
