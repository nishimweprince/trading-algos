#!/bin/bash
set -euo pipefail
DEST="traderbot@<IP>"
DEST_DIR="/home/traderbot/nadobot"
PORT="2222"
LOCAL_DIR="/Users/lifo/ords/bitcoin9to5"
rsync -avz --delete \
  --exclude node_modules \
  --exclude .git \
  --exclude .env \
  --exclude .bot-state.json \
  --exclude .market-data.json \
  --exclude .zone-config.json \
  --exclude '*.tgz' \
  -e "ssh -p ${PORT}" \
  "${LOCAL_DIR}/" "${DEST}:${DEST_DIR}/"
ssh -p "${PORT}" "${DEST}" "cd ${DEST_DIR} && npm install && pm2 reload ecosystem.config.cjs || pm2 start ecosystem.config.cjs"
