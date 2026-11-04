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
