#!/usr/bin/env bash
# Executor on a local validator with cloned mainnet accounts (spec §8 testing, §13 phase 3). VM step:
# needs the Solana/Agave CLI (solana-test-validator, cargo build-sbf) and a mainnet RPC to clone from.
#
#   program/executor/tests/validator.sh WALLET_PUBKEY [RPC_URL]
#
# WALLET_PUBKEY is the public wSOL holder sim-check uses (it prints it; pass the same with --wallet). The
# executor is built with OWNER = WALLET_PUBKEY (sigVerify is off in simulation, so no key is needed).
# Checks:
#   1. the full §7.3 transaction (Kamino borrow, execute_route over a Raydium round trip, repay, tip)
#      succeeds when min_out equals what the route returns;
#   2. the same with min_out + 1 reverts with MinOutNotMet (custom error 6002 / 0x1772).
set -euo pipefail
WALLET=${1:?wallet pubkey}
RPC=${2:-https://api.mainnet-beta.solana.com}
HERE=$(cd "$(dirname "$0")" && pwd)
CRATE=$(cd "$HERE/.." && pwd)
ROOT=$(cd "$CRATE/../.." && pwd)
EXEC_KEYPAIR="$CRATE/target/deploy/jito_arb_executor-keypair.json"

# 1. Build with OWNER set to the test wallet (restored afterwards).
cp "$CRATE/src/processor.rs" "$CRATE/src/processor.rs.bak"
trap 'mv "$CRATE/src/processor.rs.bak" "$CRATE/src/processor.rs"' EXIT
sed -i.tmp "s|pub const OWNER: Pubkey = pubkey!(\"[^\"]*\");|pub const OWNER: Pubkey = pubkey!(\"$WALLET\");|" "$CRATE/src/processor.rs"
rm -f "$CRATE/src/processor.rs.tmp"
(cd "$CRATE" && cargo test && cargo build-sbf)
EXEC_ID=$(solana-keygen pubkey "$EXEC_KEYPAIR")

# 2. Accounts to clone: every account sim-check's transactions touched on mainnet (recorded in
#    tests/fixtures/sim_check.json by a mainnet run), minus builtins; programs are cloned as upgradeable.
#    Run `build/release/sim-check --wallet WALLET` against mainnet first so the list matches this wallet.
SKIP="11111111111111111111111111111111 TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL Sysvar1nstructions1111111111111111111111111 ComputeBudget111111111111111111111111111111"
PROGRAMS="675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8 KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD"
ARGS=()
for k in $(python3 -c "import json,sys; print(' '.join(json.load(open(sys.argv[1]))['accounts']))" "$ROOT/tests/fixtures/sim_check.json"); do
  case " $SKIP $PROGRAMS $EXEC_ID " in *" $k "*) continue ;; esac
  ARGS+=(--maybe-clone "$k")
done
for p in $PROGRAMS; do ARGS+=(--clone-upgradeable-program "$p"); done

solana-test-validator --reset --quiet --url "$RPC" "${ARGS[@]}" \
  --bpf-program "$EXEC_ID" "$CRATE/target/deploy/jito_arb_executor.so" &
VAL=$!
trap 'kill $VAL 2>/dev/null; mv "$CRATE/src/processor.rs.bak" "$CRATE/src/processor.rs"' EXIT
for _ in $(seq 60); do solana -u http://127.0.0.1:8899 cluster-version >/dev/null 2>&1 && break; sleep 1; done

SIM="$ROOT/build/release/sim-check"
"$SIM" --rpc http://127.0.0.1:8899 --root "$ROOT" --executor "$EXEC_ID" --wallet "$WALLET"
"$SIM" --rpc http://127.0.0.1:8899 --root "$ROOT" --executor "$EXEC_ID" --wallet "$WALLET" --tamper
echo "validator checks passed: profitable route succeeds, tampered min_out reverts"
