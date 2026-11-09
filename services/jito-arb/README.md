# jito-arb

Solana atomic cyclic-arbitrage bot in C++ (spec: [docs/jito-arb.md](docs/jito-arb.md)). The bot reads
pool state from LaserStream, prices 2- and 3-hop cycles with exact quoters, sizes and prices each one,
and builds one transaction: a Kamino flash borrow, an `execute_route` call into an on-chain executor that
reverts unless `min_out` is met, the repay, and a Jito tip. That transaction goes out as a one-transaction
Jito bundle.

**Status: phases 0–3 are built and tested offline. Nothing has ever been sent to mainnet.** `dry_run = true`
is the default, replay mode is always a dry run, and in dry run the submitter holds no Jito client at all.
The VM steps (live feed, recording, the phase-2 replay gate, the executor build and validator tests) are in
[RUNBOOK-VM.md](RUNBOOK-VM.md). No mainnet send should happen before the owner has reviewed the gate.

## Documentation

| Document | What it covers |
|---|---|
| [docs/jito-arb.md](docs/jito-arb.md) | The full spec: architecture (§2), Helius/Jito/hosting (§3), market data and DEX list (§4), exact quoters (§5), decision engine, sizing, cost model and tips (§6), Kamino flash loans and the tx layout (§7), the Rust executor (§8), building and submitting (§9), risk limits (§10), observability and replay (§11), layout and config (§12), phases and acceptance criteria (§13) |
| [docs/implementation-plan.md](docs/implementation-plan.md) | How phases 0–3 were built offline: what was reused from pump-desk, the per-phase work list, the guardrails for the unattended run, and the end-of-run checks |
| [Deployment](#deployment) (below) | Setting up the services and the dashboard, with every env variable they need |
| [RUNBOOK-VM.md](RUNBOOK-VM.md) | Frankfurt VM steps 0–11: install, build, secrets, Whirlpool and SDK parity, discovery, 24 h LaserStream hold, 3-day recording, the phase-2 replay gate, executor validator tests, devnet ALTs, owner review |
| [config/bot.example.toml](config/bot.example.toml) | Every config key with its default (defaults live in `src/core/config.hpp`) |
| [config/dex_programs.toml](config/dex_programs.toml) | Pinned program IDs and the source commits each quoter/decoder was ported from |
| [config/denylist.json](config/denylist.json) | Mints and pools the bot must never touch |
| [deploy/](deploy/) | `install.sh`, the VM config `bot.vm.toml`, the env template, and systemd units: `jito-arb` (live dry run), `jito-arb-discover` (+ 10-minute timer), `jito-arb-recorder`, `jito-arb-ui` |
| [ui/server.py](ui/server.py) | Read-only dashboard on 127.0.0.1:8088 (metrics, decision log, discovery summary); the docstring lists its endpoints |
| [harness/](harness/) | `sim_parity.cpp` (simulation oracle), `holders_seed.json`, `idl/`, and `parity-node/` (official-SDK parity harness, VM only) |
| [program/executor/](program/executor/) | On-chain executor (Rust) and `tests/validator.sh` |

## DEX coverage (where is Orca?)

Orca's DEX is **Whirlpool** (program `whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc`); every "Whirlpool" in
this repo is Orca. The spec (§4) plans six DEXes in three phases. Only the phase-1 pair is built:

| DEX | Model | Decoder | Quoter | Parity | Trading |
|---|---|---|---|---|---|
| Raydium AMM v4 | Constant product | Built | Built (`src/dex/raydium_v4.cpp`) | 313/313 exact | **Enabled** (`raydium_v4_enabled = true`) |
| Orca Whirlpool | Concentrated liquidity | Built (pool + tick arrays) | Built (`src/dex/whirlpool.cpp`, static-fee pools only; adaptive-fee pools refused) | **Not proven**: 10/10 exact on one pool, far under the 200-sample bar | **Disabled** (`whirlpool_enabled = false`). Discovery still tracks its pools (`discovery.track_whirlpool = true`) so the VM streams their state, but no cycle through a Whirlpool is quoted |
| Raydium CPMM | Constant product, Token-2022 aware | — | — | — | Spec phase 2, not built |
| Meteora DLMM | Discrete bins | — | — | — | Spec phase 2, not built |
| Raydium CLMM | Concentrated liquidity | — | — | — | Spec phase 3, not built |
| PumpSwap AMM | Constant product | — | — | — | Spec phase 3, not built |

To turn Orca on: run the Helius-funded sim-parity run (RUNBOOK-VM.md step 4) until it has at least 200
exact samples and no mismatches, store them in `tests/fixtures/`, then set `whirlpool_enabled = true`.
The test `parity_whirlpool_exact` fails if Whirlpool is enabled without that proof. On the VM, cycles are
limited by the token graph (a mint needs a second pool), so adding Orca, then PumpSwap, is the main way
to get more cycles.

## Profitability formula

This is what `src/engine/sizer.cpp` and `src/engine/costs.cpp` compute for each cycle (spec §6.3–6.8).
All arithmetic is integer: raw base-token units, lamports, and parts per million. A base-token amount is
converted to lamports at the mid price of the deepest pool against SOL (wSOL = 1).

**1. Prefilter (spot edge).** With `r_i` the leg's mid price (Raydium: `R_out / R_in` using vault minus
`need_take_pnl`; Whirlpool: `sqrt_price² / 2^128`, or its inverse) and `f_i` the pool fee:

```
spot_edge = Π_i  r_i · (1 − f_i)  −  1        sized only if spot_edge ≥ min_spot_edge (0.0005 = 5 bps)
```

**2. Gross profit at size x** (in base units). `out_i` is the exact on-chain quote, so DEX fees and
rounding are already inside it (Raydium: `fee = ceil(in · num / den)`, `out = floor(R_out · in' / (R_in + in'))`).

```
flash_fee(x) = max(x · fee_sf / 2^60, 1), rounded half up      (Kamino; fee_sf read live from the reserve;
                                                                fee_sf = u64::MAX means flash loans off)
π(x)         = out_n( … out_1(x) … )  −  x  −  flash_fee(x)
```

**3. Size.** `x*` maximises π(x) over `[min_trade, x_max]` by golden-section search (≤ 24 quotes), seeded
with the closed-form optimum for 2-hop Raydium cycles. With `a = R_in1 · R_in2`,
`S = √(n1 n2 · R_out1 R_out2 · R_in1 R_in2)` and `n_i = den_i − num_i` (the fraction the pool keeps):

```
x*_seed = (S · √(d1 d2) − a · d1 d2) / (n1 · (d2 · R_in2 + n2 · R_out1))
x_max   = min(flash reserve liquidity (or own balance), max_input_fraction (20%) × shallowest leg depth, max_trade)
```

A size that crosses a tick-array or liquidity boundary is rejected.

**4. Costs, tip and net** (lamports; `gross = π(x*)` in lamports):

```
CU       = (Σ leg CU + 60,000 overhead) × 1.15                must be ≤ 1,200,000
fees     = 5,000 base fee + cu_price × CU / 10^6               (cu_price = 0 on the bundle path)
tip_min  = max(tip_min_lamports (1,000), Jito tip floor p25)
tip      = clamp(α_k · gross, tip_min, tip_max (0.05 SOL))
tip      = min(tip, gross − fees − min_net)                    never lets the tip eat the floor profit
net      = gross − tip − fees
EV       = p_land · net − (1 − p_land) · c_fail                c_fail = 0 on the Jito bundle path
SEND if  gross − fees − min_net ≥ tip_min  AND  EV ≥ min_ev   (min_net = 10,000, min_ev = 20,000 lamports)
```

`α_k` is the tip share for route class `k` (hops plus DEX mix). It starts at 0.5, and every 50 attempts it
moves by 0.05 toward a 30% landing rate, bounded to [0.2, 0.95]. `p_land` is the class's landing rate,
smoothed toward 0.10 with 10 pseudo-attempts.

**5. On-chain guard.** The executor reverts the whole transaction (flash borrow included) unless the
cycle output reaches

```
min_out = x* + flash_fee + ceil(tip in base units) + ceil(min_net in base units)      must be ≤ x* + flash_fee + π(x*)
```

So a bundle that lands keeps at least `min_net` after repaying the loan and paying the tip. A bundle that
does not land costs nothing, because the tip is paid inside the arb transaction.

**Worked example** (made-up numbers for a SOL-based cycle, assuming a 1 bp flash fee and a tip floor
under the computed tip):

| Step | Value |
|---|---|
| x* | 10 SOL = 10,000,000,000 |
| Cycle output | 10,003,000,000 |
| flash_fee (1 bp) | 1,000,000 |
| gross π | 10,003,000,000 − 10,000,000,000 − 1,000,000 = 2,000,000 |
| fees | 5,000 |
| tip = 0.5 × gross | 1,000,000 (cap 2,000,000 − 5,000 − 10,000 = 1,985,000, so not capped) |
| net | 2,000,000 − 1,000,000 − 5,000 = 995,000 lamports ≈ 0.001 SOL |
| EV at p_land = 0.10 | 99,500 ≥ 20,000, so it is sent |
| min_out | 10,000,000,000 + 1,000,000 + 1,000,000 + 10,000 = 10,002,010,000 |

**Smallest opportunity that gets sent.** Two checks bind:

- Guard: `gross ≥ fees + min_net + tip_min = 5,000 + 10,000 + 1,000 = 16,000` lamports beyond the flash fee.
- EV: `p_land · net ≥ min_ev`. At the starting `p_land = 0.10` and `α = 0.5`, `net ≈ 0.5 · gross`, so
  `gross ≥ 2 · 20,000 / 0.10 ≈ 400,000` lamports (0.0004 SOL) beyond the flash fee. This is the bar that
  binds until the bot has its own landing history. As `p_land` rises, the bar falls toward the guard floor.

## Build and test

Laptop (macOS, Homebrew libsodium, simdjson, openssl; no gRPC or Rust):

```bash
cmake --preset release && cmake --build --preset release
ctest --preset release                      # 45 tests: unit, decoders, parity replay, golden tx, risk, dry-run e2e
./build/release/arb_bench                   # latency microbenchmarks
./build/release/jito-arb --config config/bot.example.toml --mode replay \
    --replay tests/fixtures/frames.bin --pools tests/fixtures/replay_pools.json --decisions /tmp/dec.bin
./build/release/replay-report /tmp/dec.bin
```

On the VM, the `vm` preset adds the LaserStream gRPC transport (`ARB_WITH_GRPC=ON`, needs grpc++).

Tools: `discover` (pool discovery, gPA through Helius), `recorder` (frame files), `fixture-frames`
(synthetic replay capture), `replay-report`, `sim-parity` (quoter oracle), `sim-check` (Kamino and swap
simulations). Tools that talk to mainnet do reads and `simulateTransaction` only.

## Deployment

The production host is a Linux VM in Frankfurt (Amazon Linux 2023 today; Amsterdam is the fallback).
There are two parts, and you can deploy them separately:

- **Services**: the C++ bot (`jito-arb`) plus pool discovery and the optional recorder. They run as
  systemd units under the `jito-arb` user.
- **Frontend**: the read-only dashboard (`ui/server.py` + `ui/index.html`). It is Python 3.9+ standard
  library only, with no build step and no npm.

Everything below is a dry run. The build refuses `dry_run = false`, so nothing is ever sent to mainnet.

| Unit | What it does | Needs |
|---|---|---|
| `jito-arb.service` | Live feed and engine, `--mode live --dry-run`; `/metrics` on 127.0.0.1:9100 | env file, `pools.json` |
| `jito-arb-discover.service` + `.timer` | Pool discovery (gPA through Helius) every 10 min (first run 1 min after boot); writes `pools.json` | env file |
| `jito-arb-recorder.service` | Optional. Raw LaserStream frames to `/var/lib/jito-arb/frames` (1–3 GB/hour) | env file, `pools.json` |
| `jito-arb-ui.service` | Dashboard on 127.0.0.1:8088 | `jito-arb.service` metrics, read access to `/var/lib/jito-arb` |

### Environment variables

The services read their secrets from **`/etc/jito-arb/jito-arb.env`** (`root:root`, mode 0600). Every
bot unit loads it through `EnvironmentFile=`. `install.sh` creates it from
[deploy/jito-arb.env.example](deploy/jito-arb.env.example) and never overwrites it afterwards. Fill it
in before you start anything:

| Variable | Required | Used by | What it is |
|---|---|---|---|
| `HELIUS_API_KEY` | **Yes** | `jito-arb`, `discover`, `recorder`, `sim-parity`, `sim-check`, `quote-probe` | Helius API key. It is the LaserStream `x-token` and goes on the RPC URL (`https://mainnet.helius-rpc.com/?api-key=…`). If it is missing, RPC silently falls back to the public `api.mainnet-beta.solana.com` and LaserStream has no token, so always set it. You can change the variable's name with `helius.api_key_env` in `bot.toml`. |
| `LASERSTREAM_ENDPOINT` | **Yes** for `live` and `recorder` | `jito-arb` | The Frankfurt LaserStream gRPC endpoint from the Helius dashboard, e.g. `https://laserstream-mainnet-fra.helius-rpc.com`. It overrides `helius.laserstream_endpoint` in `bot.toml`, which ships as `"VERIFY"`. |

```bash
# /etc/jito-arb/jito-arb.env
HELIUS_API_KEY=<your Helius key>
LASERSTREAM_ENDPOINT=https://laserstream-mainnet-fra.helius-rpc.com
```

Variables used only by one-off tools, never by the services:

| Variable | Used by |
|---|---|
| `RPC_URL` | `program/executor/tests/cloned_validator.py` (the RPC to clone mainnet accounts from) |
| `PREPROC_ENDPOINT`, `PREPROC_ACCOUNTS_FILE`, `PREPROC_TOKEN` | `tools/preprocessed-probe` (research probe) |

**The dashboard needs no environment variables.** It holds no secrets and is configured only by its
command-line flags (below).

No Jito credentials are needed: the bundle path uses Jito's public endpoints. A hot wallet is not
needed for a dry run either. If `/etc/jito-arb/hot_wallet.json` is absent, a fixed public test key signs.

To run a tool by hand with the same environment as the services:

```bash
sudo -u jito-arb env $(sudo cat /etc/jito-arb/jito-arb.env | grep -v '^#' | xargs) \
  /opt/jito-arb/bin/discover --config /etc/jito-arb/bot.toml --out /tmp/pools.json
```

### 1. Services

**Host packages** (Amazon Linux 2023 / Fedora; use the Debian equivalents elsewhere; grpc++ ≥ 1.51 and
a compiler with C++23 `<expected>`, i.e. GCC 13+ or Clang 17+):

```bash
sudo dnf install -y git gcc-c++ cmake ninja-build pkgconf-pkg-config \
  libsodium-devel openssl-devel simdjson-devel grpc-devel grpc-plugins protobuf-devel python3
```

**Build and test** (the `vm` preset turns on the real LaserStream gRPC client):

```bash
git clone https://github.com/nishimweprince/trading-algos.git && cd trading-algos/services/jito-arb
git rev-parse HEAD                                    # keep this with the rollout evidence
cmake --preset vm && cmake --build --preset vm
ctest --test-dir build/vm --output-on-failure
python3 deploy/tests/test_deploy.py                   # install/reset scripts against a fake systemctl
```

**Install** (as root, from the repository root). This installs the binaries to `/opt/jito-arb/bin`,
the config to `/etc/jito-arb`, the data directories under `/var/lib/jito-arb`, the dashboard to
`/opt/jito-arb/ui`, and the systemd units. It creates the `jito-arb` system user. It is idempotent and
never overwrites an existing `bot.toml`, env file or denylist. It does not start or restart anything.

```bash
cd ~/trading-algos
sudo services/jito-arb/deploy/install.sh
```

**Configure:**

```bash
sudo vi /etc/jito-arb/jito-arb.env          # HELIUS_API_KEY and LASERSTREAM_ENDPOINT (table above)
sudo vi /etc/jito-arb/bot.toml              # from deploy/bot.vm.toml; dry_run must stay true
```

`bot.toml` is [deploy/bot.vm.toml](deploy/bot.vm.toml). Every key and its default is listed in
[config/bot.example.toml](config/bot.example.toml), and unknown keys are rejected at startup. Check the
paths (`pools_path`, `obs.decision_log_dir`, `recorder.dir`) if your data disk is mounted elsewhere.

**Start**, in this order. The bot and recorder will not start until `pools.json` exists
(`ConditionPathExists`):

```bash
sudo systemctl start jito-arb-discover.service          # first pools.json (oneshot; takes a few minutes)
ls -l /var/lib/jito-arb/pools.json
sudo systemctl enable --now jito-arb-discover.timer jito-arb.service
sudo systemctl enable --now jito-arb-recorder.service   # optional: only when recording for the replay gate
```

**Verify:**

```bash
systemctl is-active jito-arb.service jito-arb-discover.timer
journalctl -u jito-arb -f                               # feed connect, pool updates, dry-run decisions
journalctl -u jito-arb-discover --since -15min
curl -s 127.0.0.1:9100/metrics | grep -E '^arb_(feed_frames|engine_candidates|kill)'
```

### 2. Frontend (dashboard)

The dashboard samples the bot's `/metrics` every 5 s into a 6-hour buffer. It reads the decision logs
and `pools.json` from the data directory, and asks `systemctl` for the unit states. It writes nothing
and has **no login**, so bind it to localhost and put access control in front of it.

| Flag | Default | Meaning |
|---|---|---|
| `--bind` | `127.0.0.1` | Listen address |
| `--port` | `8088` | Listen port |
| `--metrics` | `http://127.0.0.1:9100/metrics` | The bot's Prometheus endpoint (`obs.metrics_addr`/`metrics_port`) |
| `--data` | `/var/lib/jito-arb` | Data directory: `decisions/`, `pools.json` |

**On the bot host, as a service** (`install.sh` has already copied `server.py` and `index.html` to
`/opt/jito-arb/ui`):

```bash
sudo systemctl enable --now jito-arb-ui.service
curl -s 127.0.0.1:8088/api/state | head -c 300
```

**Fonts (optional).** Anthropic Sans/Mono are proprietary and never committed. To use them, copy
`AnthropicSans-Roman-Web.woff2` and `AnthropicMono-Roman-Web.woff2` into `services/jito-arb/ui/fonts/`
on the host, re-run `install.sh`, then run `sudo systemctl restart jito-arb-ui`. Without them, the page
uses system fonts.

**Update only the frontend.** This does not touch the bot:

```bash
cd ~/trading-algos && git pull
sudo install -m 0644 services/jito-arb/ui/server.py services/jito-arb/ui/index.html /opt/jito-arb/ui/
sudo systemctl restart jito-arb-ui.service
```

**Open it.** Use either of these:

```bash
ssh -L 8088:127.0.0.1:8088 <user>@<vm>          # then browse http://127.0.0.1:8088
# or publish through a tunnel with access control in front, e.g. Cloudflare Tunnel + Access:
cloudflared tunnel --url http://127.0.0.1:8088
```

**Run it off the bot host** (a laptop, for development). Tunnel the metrics port and copy the data you
want to see. Unit states show as `no systemd`:

```bash
ssh -L 9100:127.0.0.1:9100 <user>@<vm>
rsync -a <user>@<vm>:/var/lib/jito-arb/{pools.json,decisions} ./arb-data/
python3 ui/server.py --bind 127.0.0.1 --port 8088 --metrics http://127.0.0.1:9100/metrics --data ./arb-data
python3 ui/test_server.py                       # dashboard unit tests
```

### Redeploy, reset, stop

```bash
cd ~/trading-algos && git pull && git rev-parse HEAD
cd services/jito-arb && cmake --build --preset vm && ctest --test-dir build/vm --output-on-failure && cd ../..
sudo services/jito-arb/deploy/install.sh       # new binaries, units; preserves config, env, denylist
sudo services/jito-arb/deploy/reset-stats.sh   # stop, archive decisions/frames, restart bot + UI (+ recorder if it was running)
```

`install.sh` does not restart anything, and `reset-stats.sh` is the restart that picks up the new
binaries. If you only want a restart and do not need the logs archived, run
`sudo systemctl restart jito-arb jito-arb-ui`. If you changed the systemd units, run
`sudo systemctl daemon-reload` first (`install.sh` already does this).

| Action | Command |
|---|---|
| Block every bundle immediately (kill switch, checked each pass) | `sudo -u jito-arb touch /var/lib/jito-arb/KILL` (remove it to clear) |
| Stop everything | `sudo systemctl disable --now jito-arb jito-arb-recorder jito-arb-ui jito-arb-discover.timer` |
| Logs | `journalctl -u jito-arb -u jito-arb-discover -u jito-arb-recorder -u jito-arb-ui` |

### Files on the host

| Path | Contents | Owner, mode |
|---|---|---|
| `/opt/jito-arb/bin/` | `jito-arb`, `discover`, `recorder`, `replay-report`, `sim-parity`, `sim-check`, `quote-probe`, … | root, 0755 |
| `/opt/jito-arb/share/` | `dex_programs.toml`, `denylist.json`, `holders_seed.json` | root, 0644 |
| `/opt/jito-arb/ui/` | `server.py`, `index.html`, `fonts/` | root, 0644 |
| `/etc/jito-arb/bot.toml` | Bot config | root:jito-arb, 0640 |
| `/etc/jito-arb/jito-arb.env` | Secrets (`HELIUS_API_KEY`, `LASERSTREAM_ENDPOINT`) | root:root, 0600 |
| `/etc/jito-arb/hot_wallet.json` | Optional; absent for dry runs | 0600 |
| `/var/lib/jito-arb/` | `pools.json`, `decisions/`, `frames/`, `config/denylist.json`, `KILL`, `archive/` | jito-arb, 0750 |

The research steps that follow deployment (24 h LaserStream hold, 3-day recording, replay gate, executor
validator tests, ALTs) are in [RUNBOOK-VM.md](RUNBOOK-VM.md).

## What is built, and how it was verified here

| Phase | Built | Verified offline (laptop) |
|---|---|---|
| 0 | Core types, base58/64, u256, compact-u16, TOML config, logger, `/metrics` on 127.0.0.1:9100, KILL file | Known vectors; `jito-arb --dry-run` serves metrics |
| 1 | Account decoders (Raydium v4, Whirlpool, tick arrays, SPL/Token-2022 with extensions, Kamino reserve), StateStore (RCU snapshots, SPSC change queue, quotability), subscription plan, resync, discovery, token safety, recorder | Decoders match spl-token / Anchor IDL / Raydium API on real fixtures; recorder round trip; discovery on recorded gPA |
| 2 | Exact quoters (Raydium v4, Whirlpool static-fee path), route graph and cycle index, prefilter, sizer, cost model, tip controller, dispatcher, decision log, replay and report | **Raydium v4: 313/313 exact** against simulated mainnet swaps, plus a live Kamino+Raydium round trip predicted to the unit. **Whirlpool: 204/204 exact** (Helius-funded run on the VM). Engine tests on synthetic and fixture states; replay of a synthetic capture to a two-day report |
| 3 | Instruction builders, §7.3 route with computed `borrow_instruction_index`, ALT-aware v0 compiler, ALT manager, blockhash cache, sign guard, §10 limits, denylist, Jito client, bundle tracker, dry-run gate, Rust executor crate | **Byte-identical** to web3.js 1.99 goldens (0, 1, 2 lookup tables; ALT create+extend; signatures); `sim-check` on mainnet: Kamino fee exact, Kamino around a Raydium round trip exact, the full route rejected only for the undeployed executor; dry-run replay signs 50/50 bundles with zero transport calls |

Laptop benchmark (Apple M-series, single thread; production numbers are a VM step):

| Path | Measured | Spec target |
|---|---|---|
| Raydium v4 quote | 29 ns | < 100 ns |
| Whirlpool quote (≤ 5 ticks, 52 cases) | 166 ns | < 2 µs |
| Pool update → engine pass | p50 0.3 µs, p99 3.2 µs (one cold-cache 1.7 ms max) | < 200 µs |

### Quoter parity

There are no Raydium/Orca SDKs on the laptop, so parity uses a **simulation oracle**
(`harness/sim_parity.cpp`). For each fixture pool and size, it builds the direct swap the executor would
CPI and runs `simulateTransaction` (sigVerify off) from a public holder's wallet. It reads the output
account, and keeps a sample only when the pool, vaults and tick arrays are byte-identical across a
`getMultipleAccounts` bracket *and* the simulated vault deltas equal the user's (vault conservation). The
unit test replays the stored samples offline and requires exact `amount_out` and `amount_in`.

- Raydium v4: 313 samples in 16 batches, 0 mismatches (`tests/fixtures/parity_raydium_v4.json`). The
  run that recorded them predates one oracle fix: a failed simulation now counts only if the funding
  wallet did not move. The single failed sample in that file was a holder spending mid-batch (Raydium's
  `user_source.amount < amount_in`, error 40). It was dropped and is listed in the file.
- Whirlpool: 204 samples in 11 batches (21 pool-direction pairs), 0 mismatches
  (`tests/fixtures/parity_whirlpool.json`), recorded on the Frankfurt VM with Helius-funded holders. The
  laptop's public RPC could not fund wSOL/USDC swaps. The 40 failed simulations from that run failed at
  instruction 0 (the harness creating a classic-SPL ATA for a Token-2022 mint), before the swap ran; they are
  dropped and listed in the file, the rule sim-parity now applies. Quoting is on (`whirlpool_enabled = true`).
  The test still enforces that a DEX under 200 exact samples stays disabled. The spec's 1,000 × 20 against
  the official SDKs (`harness/parity-node`) remains a VM step.

## VERIFY items

| Item | Finding | Source |
|---|---|---|
| Program IDs (Raydium v4, Whirlpool, Kamino) | Confirmed executable on mainnet; pinned with source commits | `config/dex_programs.toml` |
| Raydium v4 fee and rounding | Fee = ceil(in × num / den) on input; out floors; reserves = vault − need_take_pnl | raydium-amm@91739d8 `processor.rs`, `math.rs`; parity 313/313 |
| Whirlpool swap math | Ported verbatim (tick sequence, U256 muldiv, rounding); adaptive-fee pools refused | whirlpools@cc683ec; parity 204/204 |
| Kamino flash fee | `max(amount × fee_sf / 2^60, 1)` rounded half-up; `u64::MAX` = disabled; `BorrowTooSmall` | klend@77e8185 `reserve.rs calculate_fees`; sim-check (a) exact |
| Kamino CPI and pairing rules | Borrow and repay must be top level (`is_flash_forbidden_cpi_call`); exactly one repay after the borrow, same amount, **identical account list**, absolute `borrow_instruction_index` | klend@77e8185 `lending_market/flash_ixs.rs` |
| Jito `sendBundle` | `[[base64 txs], {"encoding":"base64"}]` to `/api/v1/bundles`; up to 5 txs; min tip 1,000 lamports | docs.jito.wtf/lowlatencytxnsend (read 2026-11-04) |
| Jito bundle status | `getInflightBundleStatuses` (Invalid/Pending/Failed/Landed, 5-minute look-back) and `getBundleStatuses`, ≤ 5 ids per call | same |
| Jito rate limit | Default 1 request/s per IP per region; HTTP 429 when exceeded | same |
| Jito tip floor | `GET bundles.jito.wtf/api/v1/bundles/tip_floor`, `landed_tips_25th_percentile` in SOL | same |
| Tip accounts in an ALT | **Not allowed** ("make sure to not use Address Lookup Tables for the tip accounts"): the compiler keeps the tip account static | same |
| Tip accounts | The 8 documented accounts are built in; `getTipAccounts` refreshes them hourly | same |
| ComputeBudget encodings | SetComputeUnitLimit `[2, u32]`, SetComputeUnitPrice `[3, u64]` | web3.js goldens, byte-identical |
| ALT account layout | 56-byte LookupTableMeta then addresses; activation after `last_extended_slot` | Solana address-lookup-table program state. **VERIFY on the VM** by decoding a real table (the instruction encodings are golden-tested) |
| LaserStream endpoint, `from_slot` replay, connection limits | **Unresolved**: `laserstream_endpoint = "VERIFY"`, `replay_from_slot = false` | Helius dashboard (VM step) |
| Helius Sender minimum tip | **Unresolved**: the Sender path is off (`sender_enabled = false`) | — |
| marginfi fallback | Not built (phase 6) | — |

## Deviations from the spec

- **Libraries.** These are laptop-installable stand-ins, copied from pump-desk's tested code. A TOML
  subset parser stands in for tomlplusplus, a small async logger for spdlog, and a hand-written
  `/metrics` endpoint for prometheus-cpp. An SPSC ring replaces concurrentqueue and rigtorp. A minimal
  test harness and chrono benchmarks stand in for GoogleTest and Google Benchmark. The build uses
  Homebrew with `find_library` fallbacks instead of vcpkg.
- **C++23.** The language is C++20 practice, but the standard is set to 23 for `std::expected`.
- **Parity oracle.** Parity uses `simulateTransaction` instead of the SDKs on the laptop (above). The
  SDK harness exists for the VM.
- **Executor instruction.** The data starts with a 1-byte tag (0 = `execute_route`) before the §8 struct
  (30 bytes total). Each leg's account slice begins with the DEX program account, which the CPI needs.
- **Region fan-out.** It is sequential over kept-alive connections on the submit thread, not parallel
  sockets. That is enough at Jito's 1 req/s/region default; revisit when a higher limit is granted.
- **Replay and dry run.** These sign over a placeholder blockhash with *virtual* lookup tables (one per
  pool plus a shared one, as discovery would create them). The signatures are real (fixed test key) but
  reference no on-chain tables.
- **Funding mode.** Own-capital mode caps `x_max` at the wallet's base balance, which the operator sets
  through `set_own_balance` until the wallet subscription feeds it (VM).

## Alpenglow (live on mainnet since 2026-10-21)

- **Competing banks.** A slot can carry more than one candidate block, and Geyser tags each update with
  its `bank_id` (`SubscribeUpdateAccount` field 4, `SubscribeUpdateSlot` field 5). The feed tracks banks per
  slot. While a slot has updates from two or more banks, the accounts touched in it are not quotable. When
  the slot's bank is confirmed, every account a losing bank wrote is re-read by trusted RPC.
  `bank_id` is node-local, so it is compared only within one connection.
- **200 ms slots.** Time-based limits are in milliseconds and converted with `chain.slot_ms` (200):
  `max_send_lag_ms`, `cooldown_ms`, `resync_lag_ms`. Limits the chain defines in slots stay in slots
  (blockhash age, pool/vault same-slot consistency). `arb_slot_time_ms` reports the measured slot time
  and warns past 25% drift.
- Not adopted yet: block footers with finality certificates (for faster landing confirmation), and the
  `SubscribeDeshredRequest` pre-execution transaction stream (a candidate for earlier detection).

## Not built yet

- Telegram alerts and `/stop` (the KILL file works; spec §11 alerts are not wired).
- The profit sweep and USDC→SOL float top-up jobs (§7.5, §10). `Limits::sweep_excess` computes the amount.
- The tip-floor and tip-account refresh loops, and the bundle-status poller in live mode. The parsers,
  client and tracker exist and are tested, but there is nothing to poll while sends are refused.
- Pre-send `simulateTransaction` sampling and the quote-mismatch feed into `Limits::record_sim`.
- marginfi (phase 6), and DEXes beyond Raydium v4 and Whirlpool.
- The landed-arb comparison for the replay gate (RUNBOOK-VM.md step 8).

## Live and recorder modes

`--mode live` and `--mode recorder` run the LaserStream shards (1 wallet/reserve stream plus up to
`max_grpc_connections` pool streams) into one feed thread. On the laptop a stub client stands in and
yields no frames; the real client builds with the `vm` preset. Live mode is dry run only: this build
refuses `dry_run = false`.

## Layout

```
config/       bot.example.toml, dex_programs.toml (pinned IDs and commits), denylist.json
src/core      types, base58/64, u256, compact_u16, config, crypto (PDA, ATA, 0600 keypair), rpc
src/feed      LaserStream request/update codec, subscription plan, frames (recorder), resync, gRPC shim
src/state     decoders, PoolState, StateStore, discovery
src/dex       quoters (Raydium v4, Whirlpool)
src/engine    route graph, sizer, costs and tips, dispatcher, engine
src/flash     Kamino reserve and flash instructions
src/tx        v0 compiler with ALTs, DEX swap ixs, route builders, ALT manager, blockhash cache
src/submit    TLS keep-alive HTTP, Jito client, submitter (dry-run gate), bundle tracker
src/risk      token safety, sign guard, limits, denylist
src/obs       logger, metrics, decision log, replay report
src/app       runtime (feed -> engine -> log), bundle builder (send hook)
src/tools     discover, recorder, fixture-frames, replay-report, sim-check
harness/      sim-parity oracle, holders seed, parity-node (SDK harness, VM)
program/      executor (Rust, native solana-program; unbuilt here) + tests/validator.sh
tests/        unit/, fixtures/ (mainnet account bytes, parity, frames), golden_tx/, bench/
```
