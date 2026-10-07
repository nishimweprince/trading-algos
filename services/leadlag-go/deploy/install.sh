#!/usr/bin/env bash
# Run on the host as root from a checkout (or a copy of deploy/): installs the
# units and the cloudflared priority drop-in, then reloads systemd. Idempotent.
#
#   sudo deploy/install.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

install -m 0644 "$here"/systemd/leadlag.service \
                "$here"/systemd/leadlag-observer.service \
                "$here"/systemd/leadlag-status.service \
                "$here"/systemd/leadlag-research@.service /etc/systemd/system/
install -d /etc/systemd/system/cloudflared.service.d
install -m 0644 "$here"/cloudflared/priority.conf /etc/systemd/system/cloudflared.service.d/priority.conf
systemctl daemon-reload
echo "installed; enable with: systemctl enable --now leadlag-observer leadlag leadlag-status"
