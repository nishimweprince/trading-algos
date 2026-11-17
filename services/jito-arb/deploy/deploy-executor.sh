#!/usr/bin/env bash
# Build and deploy the executor program to mainnet (docs/LIVE-READINESS.md Stage 2). Run on the VM as root:
#
#   sudo services/jito-arb/deploy/deploy-executor.sh          # build, check, print the plan; asks before deploying
#
# Keys (created 17 Nov 2026):
#   /etc/jito-arb/hot_wallet.json         the hot wallet: must equal OWNER in program/executor/src/processor.rs
#   /root/jito-keys/deployer.json         pays the rent and becomes the upgrade authority
#   /root/jito-keys/executor-program.json the program's address (created here on first run)
# Afterwards set keys.executor_program_id in /etc/jito-arb/bot.toml and re-run arm-accounts --send.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(cd "$HERE/../program/executor" && pwd)
BIN=${SOLANA_BIN:-/home/ec2-user/jito-toolchains/solana-release/bin}
export PATH=$BIN:/home/ec2-user/.cargo/bin:$PATH
HOT=/etc/jito-arb/hot_wallet.json
DEPLOYER=/root/jito-keys/deployer.json
PROGRAM=/root/jito-keys/executor-program.json
set -a; . /etc/jito-arb/jito-arb.env; set +a
URL="https://mainnet.helius-rpc.com/?api-key=${HELIUS_API_KEY}"

hot=$(solana-keygen pubkey "$HOT")
owner=$(grep -oE 'pub const OWNER: Pubkey = pubkey!\("[1-9A-HJ-NP-Za-km-z]+"\)' "$SRC/src/processor.rs" | grep -oE '"[^"]+"' | tr -d '"')
[ "$owner" = "$hot" ] || { echo "OWNER in processor.rs ($owner) is not the hot wallet ($hot): refusing" >&2; exit 1; }
deployer=$(solana-keygen pubkey "$DEPLOYER")
[ "$deployer" != "$hot" ] || { echo "the deployer must not be the hot wallet" >&2; exit 1; }
[ -f "$PROGRAM" ] || { solana-keygen new --no-bip39-passphrase --silent -o "$PROGRAM" >/dev/null; chmod 600 "$PROGRAM"; }
program=$(solana-keygen pubkey "$PROGRAM")

# Build and test as the checkout's owner (never as root), in place: the tests read fixtures by repo-relative path.
# Any test or build failure stops here, before anything touches mainnet.
BUILDER=${BUILD_USER:-$(stat -c %U "$SRC")}
sudo -u "$BUILDER" env PATH="$PATH" bash -c "set -euo pipefail; cd '$SRC'; cargo test --quiet; cargo build-sbf >/dev/null 2>&1 || cargo build-sbf"
SO="$SRC/target/deploy/jito_arb_executor.so"
bytes=$(stat -c %s "$SO")
rent=$(( (bytes + 45 + 128) * 6960 ))
bal=$(solana balance "$deployer" --url "$URL" --lamports | awk '{print $1}')
existing=$(solana program show "$program" --url "$URL" >/dev/null 2>&1 && echo yes || echo no)

echo
echo "program id         $program  (already deployed: $existing)"
echo "owner (hot wallet) $hot"
echo "upgrade authority  $deployer  balance $(awk "BEGIN{printf \"%.6f\", $bal/1e9}") SOL"
echo "binary             $bytes bytes, sha256 $(sha256sum "$SO" | cut -c1-16)…"
echo "rent locked        about $(awk "BEGIN{printf \"%.4f\", $rent/1e9}") SOL, plus the same again for the deploy buffer (refunded)"
[ "$bal" -ge $(( rent * 2 + 20000000 )) ] || { echo "deployer balance too low for program + buffer + fees" >&2; exit 1; }
printf "\nDeploy (or upgrade) the executor to mainnet now? Type yes: "
read -r answer
[ "$answer" = "yes" ] || { echo "stopped: nothing deployed"; exit 1; }
solana program deploy "$SO" --url "$URL" --keypair "$DEPLOYER" --upgrade-authority "$DEPLOYER" \
  --program-id "$PROGRAM" --with-compute-unit-price 20000 --max-sign-attempts 20 --use-rpc
solana program show "$program" --url "$URL"
echo
echo "next: set keys.executor_program_id = \"$program\" in /etc/jito-arb/bot.toml,"
echo "      then re-run arm-accounts --send (adds the executor to a lookup table) and restart jito-arb."
