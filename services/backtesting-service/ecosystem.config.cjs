// pm2 deployment for the backtesting service (Linux).
// Backend is Python (workspace venv); the client is served separately
// (see client entry below).
const repo = "/home/basis/trading-algos";
const serviceDir = `${repo}/services/backtesting-service`;
const venvBin = `${repo}/.venv/bin`;

module.exports = {
  apps: [
    {
      name: "backtesting-service",
      cwd: serviceDir,
      script: `${venvBin}/python`,
      args: `${venvBin}/backtesting-service`,
      interpreter: "none",
      autorestart: true,
      max_restarts: 10,
      min_uptime: "30s",
    },
    {
      name: "backtesting-client",
      cwd: `${serviceDir}/client`,
      script: "npm",
      args: "run preview -- --port 4175 --host 127.0.0.1 --strictPort",
      autorestart: true,
      max_restarts: 10,
      min_uptime: "30s",
    },
  ],
};
