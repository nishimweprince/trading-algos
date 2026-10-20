#!/usr/bin/env bash
# One-shot setup of a fresh Ubuntu 24.04 EC2 instance in eu-central-1 for pump-desk.
# Run as the login user (sudo available) from the repo checkout:
#   bash pump-desk/deploy/aws/bootstrap.sh
# Installs build deps, tunes the kernel, builds Release with -O3 -march=native -flto,
# runs the tests, and installs the systemd units (not started).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"   # pump-desk/

echo "== packages"
sudo apt-get update -y
sudo apt-get install -y build-essential cmake ninja-build pkg-config git \
  libgrpc++-dev libprotobuf-dev libsodium-dev libssl-dev libsqlite3-dev libsimdjson-dev sqlite3 \
  linux-tools-common chrony

echo "== kernel / network tuning"
sudo install -m 0644 "$HERE/sysctl.conf" /etc/sysctl.d/90-pump-desk.conf
sudo sysctl --system >/dev/null
# Time sync against the AWS time service (latency stamps and blockhash ages rely on it).
sudo sed -i 's/^pool .*/# &/' /etc/chrony/chrony.conf
grep -q 169.254.169.123 /etc/chrony/chrony.conf || echo "server 169.254.169.123 prefer iburst minpoll 4 maxpoll 4" | sudo tee -a /etc/chrony/chrony.conf
sudo systemctl restart chrony

echo "== build"
cmake -S "$ROOT" -B "$ROOT/build" -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build "$ROOT/build" -j"$(nproc)"
(cd "$ROOT" && ./build/pd_tests)

echo "== install"
mkdir -p "$ROOT/data"
[ -f "$ROOT/pump-desk.toml" ] || cp "$ROOT/config.example.toml" "$ROOT/pump-desk.toml"
[ -f "$ROOT/.env" ] || { umask 077; cat > "$ROOT/.env" <<'ENV'
HELIUS_GRPC_URL=
HELIUS_GRPC_TOKEN=
HELIUS_HTTP_URL=
WALLET_PRIVATE_KEY=
JUPITER_API_KEY=
ENV
}
chmod 600 "$ROOT/.env"
sed -e "s#@ROOT@#$ROOT#g" -e "s#@USER@#$(id -un)#g" "$HERE/pump-desk.service" | sudo tee /etc/systemd/system/pump-desk.service >/dev/null
sed -e "s#@ROOT@#$ROOT#g" -e "s#@USER@#$(id -un)#g" "$HERE/pump-desk-status.service" | sudo tee /etc/systemd/system/pump-desk-status.service >/dev/null
sed -e "s#@ROOT@#$ROOT#g" -e "s#@USER@#$(id -un)#g" "$HERE/pump-desk-rivals.service" | sudo tee /etc/systemd/system/pump-desk-rivals.service >/dev/null
sudo install -m 0644 "$HERE/pump-desk-rivals.timer" /etc/systemd/system/pump-desk-rivals.timer
sudo systemctl daemon-reload
echo
echo "Done. Next:"
echo "  1. fill $ROOT/.env (chmod 600) and review $ROOT/pump-desk.toml"
echo "  2. preflight:  set -a; . $ROOT/.env; set +a; $ROOT/build/pump-desk --config $ROOT/pump-desk.toml --preflight"
echo "  3. isolate cores (README §Cores): isolcpus=1-3,5-7 nohz_full=1-3,5-7 rcu_nocbs=1-3,5-7"
echo "     reboot, then: sudo systemctl enable --now pump-desk pump-desk-status pump-desk-rivals.timer"
echo "     optional: list BAM validator identities in $ROOT/data/bam_validators.json (JSON array)"
echo "  4. optional tunnel, from the housekeeping core: cloudflared tunnel --url http://127.0.0.1:8787"
