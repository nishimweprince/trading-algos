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
| 2 | Exact quoters (Raydium v4, Whirlpool static-fee path), route graph and cycle index, prefilter, sizer, cost model, tip controller, dispatcher, decision log, replay and report | **Raydium v4: 313/313 exact** against simulated mainnet swaps, plus a live Kamino+Raydium round trip predicted to the unit. **Whirlpool: not proven** (see below), so disabled. Engine tests on synthetic and fixture states; replay of a synthetic capture to a two-day report |
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
- Whirlpool: **not proven**. The public RPC cannot list wSOL/USDC holders and rate-limits
  `getTokenLargestAccounts`, so the laptop run saw only 10 samples on one pool (all exact, far under the
  200 bar). `whirlpool_enabled = false` in the defaults and the example config, and
  `parity_whirlpool_exact` enforces that an unproven DEX stays disabled. The Helius-funded run, plus the
  official-SDK harness (`harness/parity-node`) for the spec's 1,000 × 20, are VM steps.

## VERIFY items

| Item | Finding | Source |
|---|---|---|
| Program IDs (Raydium v4, Whirlpool, Kamino) | Confirmed executable on mainnet; pinned with source commits | `config/dex_programs.toml` |
| Raydium v4 fee and rounding | Fee = ceil(in × num / den) on input; out floors; reserves = vault − need_take_pnl | raydium-amm@91739d8 `processor.rs`, `math.rs`; parity 313/313 |
| Whirlpool swap math | Ported verbatim (tick sequence, U256 muldiv, rounding); adaptive-fee pools refused | whirlpools@cc683ec; parity **open** |
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
