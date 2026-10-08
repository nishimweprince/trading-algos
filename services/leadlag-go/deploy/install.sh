#!/usr/bin/env bash
# Run on the host as root from a checkout (or a copy of deploy/): installs the
# units, then reloads systemd. Idempotent.
#
#   sudo deploy/install.sh                 # dedicated host (DEPLOY.md §3-4)
#   sudo deploy/install.sh --shared-host   # host shared with other services (DEPLOY.md, "Shared host")
#
# Dedicated: also installs the cloudflared priority drop-in. Shared host: adds
# drop-ins that unpin the units and keep the trader at normal priority, caps
# research jobs' memory, and leaves an existing cloudflared alone.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mode="${1:-dedicated}"

install -m 0644 "$here"/systemd/leadlag.service \
                "$here"/systemd/leadlag-observer.service \
                "$here"/systemd/leadlag-status.service \
                "$here"/systemd/leadlag-research@.service /etc/systemd/system/
case "$mode" in
  dedicated)
    install -d /etc/systemd/system/cloudflared.service.d
    install -m 0644 "$here"/cloudflared/priority.conf /etc/systemd/system/cloudflared.service.d/priority.conf
    ;;
  --shared-host)
    for unit in leadlag leadlag-observer leadlag-status; do
      install -d "/etc/systemd/system/$unit.service.d"
      install -m 0644 "$here/systemd/shared-host/$unit.conf" "/etc/systemd/system/$unit.service.d/shared-host.conf"
    done
    install -d /etc/systemd/system/leadlag-research@.service.d
    install -m 0644 "$here"/systemd/shared-host/leadlag-research.conf \
                    /etc/systemd/system/leadlag-research@.service.d/shared-host.conf
    ;;
  *)
    echo "usage: $0 [--shared-host]" >&2
    exit 2
    ;;
esac
systemctl daemon-reload
echo "installed ($mode); enable with: systemctl enable --now leadlag-observer leadlag leadlag-status"
