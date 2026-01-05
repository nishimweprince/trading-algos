#!/bin/bash
set -euo pipefail
DEST="traderbot@<IP>"
PORT="2222"
REMOTE_DIR="/home/traderbot/nadobot"
PM2_APP="nadobot"
ssh -p "$PORT" "$DEST" "cd $REMOTE_DIR && pm2 logs $PM2_APP --lines 200"
