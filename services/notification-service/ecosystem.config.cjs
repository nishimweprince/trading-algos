// pm2 deployment for the notification service (Linux).
// NOTE: `nest build` emits dist/src/main.js (no outDir override), so the
// package.json `start:prod` script (`node dist/main`) does not resolve.
// Point pm2 at the real entrypoint until that script is fixed upstream.
module.exports = {
  apps: [
    {
      name: "notification-service",
      cwd: "/home/basis/trading-algos/services/notification-service",
      script: "./dist/src/main.js",
      autorestart: true,
      max_restarts: 10,
      min_uptime: "30s",
    },
  ],
};
