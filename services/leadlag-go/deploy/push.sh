#!/usr/bin/env bash
# Build linux/amd64 binaries on this machine, upload them as a new release on
# the host, point `current` at it and restart the units (observer, trader,
# status, in that order). The previous release stays for rollback.
#
#   LL_HOST=ll@<elastic-ip> deploy/push.sh            # build, upload, switch, restart
#   LL_HOST=ll@<elastic-ip> deploy/push.sh --no-restart
set -euo pipefail

host="${LL_HOST:?set LL_HOST=ll@<elastic-ip>}"
cd "$(dirname "$0")/.."
rev="$(git rev-parse --short HEAD)"
if ! git diff --quiet HEAD -- .; then rev="$rev-dirty"; fi
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT

echo "building $rev"
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 \
  go build -trimpath -ldflags "-s -w -X main.version=$rev" -o "$out/" ./cmd/...

rel="/home/ll/leadlag/releases/$rev"
ssh "$host" "mkdir -p $rel"
scp -q "$out"/* "$host:$rel/"
ssh "$host" "ln -sfn $rel /home/ll/leadlag/current.new && mv -T /home/ll/leadlag/current.new /home/ll/leadlag/current"
echo "current -> $rel"
ssh "$host" "mkdir -p /home/ll/leadlag/jobs"
scp -q deploy/jobs/*.sh "$host:/home/ll/leadlag/jobs/"

if [[ "${1:-}" != "--no-restart" ]]; then
  ssh "$host" "sudo systemctl restart leadlag-observer leadlag leadlag-status && sleep 3 && systemctl is-active leadlag-observer leadlag leadlag-status"
fi
ssh "$host" "ls -1t /home/ll/leadlag/releases | tail -n +6 | sed 's|^|/home/ll/leadlag/releases/|' | xargs -r rm -rf"
