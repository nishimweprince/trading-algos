#!/usr/bin/env bash
# Install jito-arb on the VM from a vm-preset build: binaries to /opt/jito-arb/bin, config to /etc/jito-arb,
# data under /var/lib/jito-arb, systemd units. Idempotent; never overwrites an existing config or env file.
#   sudo services/jito-arb/deploy/install.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "install must run as root" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(cd "$HERE/.." && pwd)
BUILD="$SRC/build/vm"
[ -x "$BUILD/jito-arb" ] || { echo "build first: cmake --preset vm && cmake --build --preset vm" >&2; exit 1; }

id jito-arb >/dev/null 2>&1 || useradd --system --home-dir /var/lib/jito-arb --shell /sbin/nologin jito-arb
install -d -m 0755 /opt/jito-arb/bin /opt/jito-arb/share
install -d -m 0750 -o root -g jito-arb /etc/jito-arb
install -d -m 0750 -o jito-arb -g jito-arb /var/lib/jito-arb /var/lib/jito-arb/frames /var/lib/jito-arb/decisions

for b in jito-arb discover recorder replay-report sim-parity sim-check fixture-frames quote-probe arb_tests arb_bench; do
  [ -x "$BUILD/$b" ] && install -m 0755 "$BUILD/$b" /opt/jito-arb/bin/
done
[ -e /opt/jito-arb/share/denylist.json ] || install -m 0644 "$SRC/config/denylist.json" /opt/jito-arb/share/denylist.json
install -m 0644 "$SRC/config/dex_programs.toml" /opt/jito-arb/share/
install -m 0644 "$SRC/harness/holders_seed.json" /opt/jito-arb/share/holders_seed.json
install -d -m 0755 /opt/jito-arb/ui
install -m 0644 "$SRC/ui/server.py" "$SRC/ui/index.html" /opt/jito-arb/ui/
# Anthropic Sans/Mono are proprietary and never committed: copy AnthropicSans-Roman-Web.woff2 and
# AnthropicMono-Roman-Web.woff2 into ui/fonts/ on the host first. Without them the dashboard uses the system fonts.
if compgen -G "$SRC/ui/fonts/*.woff2" >/dev/null; then
  install -d -m 0755 /opt/jito-arb/ui/fonts
  rm -f /opt/jito-arb/ui/fonts/*.otf  # the earlier static fonts
  install -m 0644 "$SRC"/ui/fonts/*.woff2 /opt/jito-arb/ui/fonts/
fi
[ -f /etc/jito-arb/bot.toml ] || install -m 0640 -o root -g jito-arb "$HERE/bot.vm.toml" /etc/jito-arb/bot.toml
[ -f /etc/jito-arb/jito-arb.env ] || install -m 0600 -o root -g root "$HERE/jito-arb.env.example" /etc/jito-arb/jito-arb.env
# The engine reads config/denylist.json relative to its working directory.
install -d -m 0750 -o jito-arb -g jito-arb /var/lib/jito-arb/config
[ -e /var/lib/jito-arb/config/denylist.json ] || install -m 0644 "$SRC/config/denylist.json" /var/lib/jito-arb/config/denylist.json

for u in jito-arb.service jito-arb-recorder.service jito-arb-discover.service jito-arb-discover.timer jito-arb-ui.service; do
  install -m 0644 "$HERE/$u" /etc/systemd/system/
done
systemctl daemon-reload
echo "installed. Fill /etc/jito-arb/jito-arb.env, then:"
echo "  sudo systemctl start jito-arb-discover.service   # first pools.json"
echo "  sudo systemctl enable --now jito-arb-discover.timer jito-arb.service jito-arb-ui.service   # dashboard on 127.0.0.1:8088"
