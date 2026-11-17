#!/usr/bin/env bash
# Give jito-arb its own cores on a shared host (pre-live item 4, 17 Nov 2026). The Frankfurt VM also runs
# headless Chrome (tickmind, signals-scrapper), the cTrader execution service and other strategies; their bursts
# shared cores with the engine loop. This uses cgroup cpusets (`systemctl set-property ... AllowedCPUs=`), which
# apply at once without restarting anything and persist as drop-ins under /etc/systemd/system.control/.
#
#   sudo deploy/pin-cpus.sh            # jito-arb on the last two cores, every other listed unit on the rest
#   sudo deploy/pin-cpus.sh --undo     # remove the restriction from every unit touched
#
# Units that are not installed are skipped. The clock source is a separate, manual step (see RUNBOOK-VM.md).
set -euo pipefail
N=$(nproc)
if [ "$N" -lt 4 ]; then echo "pin-cpus: $N cores; need at least 4 to split, nothing done"; exit 0; fi
ARB="$((N - 2))-$((N - 1))"
REST="0-$((N - 3))"
ARB_UNITS="jito-arb.service"
OTHER_UNITS="jito-arb-discover.service jito-arb-ui.service jito-arb-recorder.service callisto-executor.service callisto-monitor.service
 callisto-ui.service execution-service.service scraper-chrome.service scraper-xvfb.service signals-scrapper.service
 solcarry-ui.service solcarry-depth.service solcarry-refresh.service tickmind-ui.service tickmind-watch.service
 walkforward-patterns.service cloudflared.service"
set_cpus() {  # unit cpus
  if systemctl cat "$1" >/dev/null 2>&1; then
    systemctl set-property "$1" AllowedCPUs="$2"
    echo "  $1 -> ${2:-all}"
  fi
}
if [ "${1:-}" = "--undo" ]; then
  for u in $ARB_UNITS $OTHER_UNITS; do set_cpus "$u" ""; done
  exit 0
fi
echo "jito-arb on CPUs $ARB, others on $REST"
for u in $ARB_UNITS; do set_cpus "$u" "$ARB"; done
for u in $OTHER_UNITS; do set_cpus "$u" "$REST"; done
