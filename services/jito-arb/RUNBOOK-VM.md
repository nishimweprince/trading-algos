# jito-arb VM runbook

These are the steps the laptop build could not do: no grpc++, Rust, Solana CLI or DEX SDKs, and no bulk
downloads on the laptop. Run them in order on a Frankfurt VM. Every step is read-only or dry run.
**No mainnet send happens in this runbook.** Phase 4 (own-capital live) starts only after the owner has
reviewed step 8 (the replay gate) and step 9 (the executor on a validator). This build refuses
`dry_run = false` outright, so going live also needs a code change the owner makes on purpose.

## 0. Host

- Frankfurt (fallback Amsterdam), 8+ vCPU, 32 GB RAM, NVMe, with a data disk for recordings (about
  1–3 GB/hour at 1,500 pools; size it for 3 days plus replays).
- Measure RTT to Helius (LaserStream and RPC) and to every Jito region before settling on the host:
  `for r in frankfurt amsterdam ny; do curl -so /dev/null -w "$r %{time_connect}\n" https://$r.mainnet.block-engine.jito.wtf; done`
- Dedicated Unix user, SSH keys only, outbound-only firewall plus the admin IP (spec §10).

## 1. Install

```bash
# Build: compiler with C++23 <expected> (GCC 13+ or Clang 17+), cmake, ninja, pkg-config, libs
sudo dnf install -y gcc-c++ cmake ninja-build pkgconf-pkg-config libsodium-devel openssl-devel simdjson-devel \
  grpc-devel grpc-plugins protobuf-devel   # or the Debian equivalents; grpc++ >= 1.51
# Rust and the Solana/Agave toolchain (2.1.x, matching program/executor/Cargo.toml)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
sh -c "$(curl -sSfL https://release.anza.xyz/v2.1.0/install)"
# Node 20+ for the SDK parity harness
```

## 2. Build and run the offline suite

```bash
cd services/jito-arb
cmake --preset vm && cmake --build --preset vm      # ARB_WITH_GRPC=ON: the real LaserStream client
ctest --test-dir build/vm --output-on-failure        # the same 45 tests as the laptop
./build/vm/arb_bench                                 # production latency numbers for the PR/report
```

The gRPC client (`src/feed/laserstream_client.cpp`) has never been compiled. Fix any build errors here
first. On the laptop the stub stands in for it.

## 3. Secrets and config

- `export HELIUS_API_KEY=...` (environment only, never in a file in git).
- Copy `config/bot.example.toml` to `/etc/jito-arb/bot.toml`. Set `laserstream_endpoint` (Frankfurt, from
  the Helius dashboard) and `pools_path = "/var/lib/jito-arb/pools.json"`, and point the `obs` and
  `recorder` dirs at the data disk.
- VERIFY in the Helius docs and dashboard: the LaserStream endpoint, the Business-plan connection limit
  (expect 10), and whether `from_slot` replay is available. Set `replay_from_slot = true` only if it is.
- A hot-wallet file is not needed for dry runs. Without one, a fixed public test key signs.

## 4. Whirlpool parity (Helius-funded) and the full SDK parity

The laptop's public RPC could not fund Whirlpool swaps: no wSOL/USDC holder lists, and holder lookups
are rate-limited. It observed 10 samples on one pool, all exact, far under the 200 bar, so the shipped
config keeps `whirlpool_enabled = false`.

```bash
./build/vm/sim-parity --dex whirlpool --seed-holders harness/holders_seed.json --min-samples 300
./build/vm/arb_tests parity                     # must stay 0 mismatches; >= 200 samples enables Whirlpool
# Spec §5: 1,000 snapshots x 20 sizes per DEX, against the official SDKs
./build/vm/sim-parity --dex all --seed-holders harness/holders_seed.json --min-samples 20000 --max-pools 1500
cd harness/parity-node && npm ci          # first confirm the pinned versions exist (npm view), then commit the lockfile
node quote.mjs ../../tests/fixtures/parity_whirlpool.json > /data/sdk_whirlpool.json
node compare.mjs ../../tests/fixtures/parity_whirlpool.json /data/sdk_whirlpool.json
node quote.mjs ../../tests/fixtures/parity_raydium_v4.json > /data/sdk_raydium.json
node compare.mjs ../../tests/fixtures/parity_raydium_v4.json /data/sdk_raydium.json
```

Any mismatch blocks that DEX: leave it disabled and investigate. Never loosen the test. Keep the
20,000-sample files on the data disk and commit only the ≤ 5 MB sets. Once Whirlpool passes, set
`whirlpool_enabled = true` in the config and the default.

## 5. Discovery

```bash
./build/vm/discover --config /etc/jito-arb/bot.toml --out /var/lib/jito-arb/pools.json
```

This checks the tracked set (≤ 1,500 pools, TVL ≥ 50 SOL, token-safety gate) and the size of the graph.
Re-run it every 10 minutes from cron. It prints the subscription diff each time.

## 5a. Redeploy and reset the stats

After `git pull` and a vm build:

```bash
sudo services/jito-arb/deploy/install.sh
sudo services/jito-arb/deploy/reset-stats.sh
```

`reset-stats.sh` stops the bot, dashboard and recorder, moves the decision logs and frames to
`/var/lib/jito-arb/archive/<UTC time>/`, and restarts them, so the dashboard starts from zero. It leaves `pools.json`,
`config/denylist.json`, any `KILL` file and `/etc/jito-arb` alone.

## 6. LaserStream: 24-hour subscription hold

```bash
./build/vm/jito-arb --config /etc/jito-arb/bot.toml --mode live      # dry run; metrics on 127.0.0.1:9100
curl -s 127.0.0.1:9100/metrics | grep -E 'arb_feed_(frames|bytes|reconnects|update_age_ms)|arb_decode_failures'
```

Pass bar: streams up for 24 h, reconnects recover by themselves, `arb_feed_update_age_ms` stays well
under 800, and decode failures stay flat. Also confirm `bytes` × 20 credits/MB fits the monthly budget.

## 7. Record 3 days

```bash
./build/vm/jito-arb --config /etc/jito-arb/bot.toml --mode recorder   # hourly files under recorder.dir
./build/vm/recorder stat /var/lib/jito-arb/frames/frames-YYYYMMDD-HHMM.bin
```

Record on the VM's data disk, never on the laptop. Watch disk usage, and keep a pools-file snapshot
from the start of the recording: replay needs the same tracked set.

## 8. The phase-2 replay gate

```bash
for f in /var/lib/jito-arb/frames/frames-*.bin; do
  ./build/vm/jito-arb --config /etc/jito-arb/bot.toml --mode replay --replay "$f" \
      --pools /var/lib/jito-arb/pools-at-recording.json --decisions /var/lib/jito-arb/replay-decisions/$(basename "$f" .bin).dec
done
./build/vm/replay-report --config /etc/jito-arb/bot.toml /var/lib/jito-arb/replay-decisions/*.dec > /var/lib/jito-arb/replay-report.txt
```

**Gate (spec §13):** about 20 or more detections per day whose net after the policy tip clears
`min_ev_lamports` (the report's `gate` column). If it is short, stop and widen scope (more DEXes,
long-tail pools) before any send code runs live.

Still to build for the gate's second half: compare detections with arbs that actually landed in the same
slots. For each detection slot, `getBlock` the slot (and the next 2), find transactions that touch the
cycle's pools and pay a Jito tip account, and report each competitor's tip and realized edge. The decision
log's `slot`, `pools` and `tip_ceil` fields carry what the comparison needs.

## 9. Executor: build, Rust tests, local validator

```bash
cd program/executor && cargo test                       # arg parsing on the shared fixture, guard math
./build/vm/sim-check --root . --wallet <WALLET>          # mainnet: records the touched accounts for cloning
program/executor/tests/validator.sh <WALLET>              # builds with OWNER=<WALLET>, clones, runs:
#   the full §7.3 tx (Kamino borrow, execute_route over a Raydium round trip, repay, tip) succeeds;
#   the same with min_out + 1 reverts with MinOutNotMet (6002 / 0x1772)
```

`<WALLET>` is the public wSOL holder sim-check prints. Simulation runs with sigVerify off, so no key is
involved. After this, set the real `OWNER` (the hot wallet) and deploy to devnet with a separate
upgrade-authority key. Keep the upgrade authority off the bot host.

## 10. Lookup tables (devnet)

On devnet, create and extend a table with `AltManager::plan_extend` (an owner-held devnet key signs and
sends). Then decode the account with `tx::decode_lookup_table` to settle the 56-byte meta layout (an
open VERIFY item), and confirm that the addresses become usable only after `last_extended_slot` + 2.

## 11. Stop: owner review

Before phase 4, the owner reviews:
- the replay report (step 8) and the landed-arb comparison;
- the executor validator results (step 9) and an audit of `program/executor/src/processor.rs`;
- parity for every enabled DEX (step 4);
- the §10 limits in `/etc/jito-arb/bot.toml`.

Phase 4 then needs the owner to remove the `dry_run = false` refusal in `src/app/live.cpp`, deliberately,
in a reviewed commit. It also needs the Jito client wired into the Submitter, own-capital mode with 0.1 to
0.5 SOL, and the KILL file and Telegram `/stop` tested live.
