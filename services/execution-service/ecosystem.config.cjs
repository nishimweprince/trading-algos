// pm2 deployment for the cTrader production gateway (Linux).
// The service is Python; point pm2 at the workspace venv interpreter
// explicitly so fork mode does not treat the entrypoint as a Node script.
const path = require("node:path");

const repo = "/home/basis/trading-algos";
const serviceDir = path.join(repo, "services", "execution-service");
const venvBin = path.join(repo, ".venv", "bin");

module.exports = {
  apps: [
    {
      name: "execution-service-production",
      cwd: serviceDir,
      script: path.join(venvBin, "python"),
      args: [path.join(venvBin, "execution-service"), "--profile", "production"].join(" "),
      interpreter: "none",
      autorestart: true,
      max_restarts: 10,
      min_uptime: "30s",
      out_file: "/home/basis/.pm2/logs/execution-service-production-out.log",
      error_file: "/home/basis/.pm2/logs/execution-service-production-error.log",
    },
  ],
};
