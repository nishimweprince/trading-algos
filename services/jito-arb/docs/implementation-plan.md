# jito-arb: C++ atomic-arb bot, phases 0–3 (offline build)

## Context

`services/jito-arb/docs/jito-arb.md` (the spec, dated 2026-11-04) describes a Solana cyclic-arbitrage bot: LaserStream feed → exact DEX quoters → cycle engine → Kamino flash-loan transaction → Jito bundle, with an on-chain Rust executor that reverts unless `min_out` is met. This run builds **phases 0–3 as code plus offline tests**, unattended, in a worktree. Nothing is sent to mainnet, and the result is never live.

Decisions from the owner (2026-11-04):
- Scope: phases 0–3, offline. Live recording, the phase-2 replay gate, SDK parity at full scale, and validator runs go into a VM runbook. They are not done here.
- Reuse: **copy** pump-desk's tested C++ into `services/jito-arb` under `arb::` and adapt it. Use pump-desk's build style (Homebrew libraries, its own test harness) instead of vcpkg/gtest. Leave pump-desk untouched.
- Network: **no installs** (no brew, npm, rustup or Solana CLI). Allowed: mainnet **RPC reads and `simulateTransaction`** for fixtures. Use `HELIUS_API_KEY` if set, else public `api.mainnet-beta.solana.com`. Assumption: read-only web fetches of official program source and IDLs (GitHub, docs.rs) are allowed, because the spec's VERIFY items require them.
- Git: branch `feat/jito-arb`, at least one commit per phase, push, and open a PR against main at the end.

### What the laptop has (checked)
- cmake, ninja, Apple clang 21 (libc++ has `std::expected`, so build with `CMAKE_CXX_STANDARD 23`), node 26.
- Homebrew: libsodium, simdjson, openssl, abseil, protobuf headers. Missing: **grpc++, pkg-config, gtest, benchmark, Rust, Solana CLI**.
- JS: `pump-desk/tools/fixtures/node_modules` already holds `@solana/web3.js` 1.99, `@solana/spl-token`, `@coral-xyz/anchor` and `@pump-fun/pump-swap-sdk` 1.20. Reuse them through `NODE_PATH` without installing anything. The Raydium, Orca, Meteora and Kamino SDKs are **not** present.

Consequences, stated up front:
- The gRPC transport shim compiles only where grpc++ exists (the VM). It sits behind an option, as pump-desk's ingest does. Everything else builds and tests on the laptop.
- The Rust executor crate is written but **not compiled**. Its build and tests are VM steps.
- Quoter parity uses a **simulation oracle** (below) on a bounded sample. The spec's 1,000 × 20 SDK parity runs on the VM through the harness written here.

## Pre-flight (worktree)

1. The spec is **untracked in main**, so the worktree will not have it. Copy `/Users/pnishimwe/Documents/Markets/Apps/trading-algos/services/jito-arb/docs/jito-arb.md` into the worktree at the same path and commit it first. Skip `.DS_Store` files.
2. Create branch `feat/jito-arb`.

## Code to copy from pump-desk (namespace `pd` → `arb::core` / `arb::tx` / `arb::feed`)

| pump-desk file | Becomes | Change |
|---|---|---|
| `src/core/{types,bytes,base58,base64,ring,key_map,sys}.hpp/.cpp` | `src/core/` | Rename namespace only |
| `src/core/pb.hpp` (zero-copy protobuf reader/writer) | `src/core/pb.hpp` | Unchanged |
| `src/core/config.{hpp,cpp}` (TOML subset) | `src/core/config.*` | New schema: spec §6.9 and §12 keys, `dry_run = true` default |
| `src/sol/crypto.{hpp,cpp}` (sodium init, on-curve, PDA, ATA, Keypair) | `src/core/crypto.*` | Keypair load enforces a `0600` file mode |
| `src/sol/tx.{hpp,cpp}` (v0 compiler, byte-identical to web3.js, no ALT) | `src/tx/message_v0.*` | **Add ALT support**, raise `MAX_IX_ACCOUNTS`/`MAX_IX_DATA`/`MAX_TX_KEYS` |
| `src/stream/request.*`, `src/stream/update.*` | `src/feed/` | Multi-shard accounts plus a slots subscription; account-update parse |
| `src/stream/ingest.*` (grpc++ generic bidi) | `src/feed/laserstream_client.*` | One instance per shard; built only with `ARB_WITH_GRPC` |
| `src/net/conn.*` (TLS keep-alive HTTP/1.1 + parser) | `src/submit/conn.*` | Unchanged |
| `src/aux/rpc.*` | `src/core/rpc.*` | Add getMultipleAccounts, getProgramAccounts, simulateTransaction, getTipAccounts |
| `tests/test.hpp` (minimal harness) | `tests/test.hpp` | Rename macros' namespace |
| `CMakeLists.txt` structure (core static lib + thin exe + test exe) | `CMakeLists.txt` | `find_path`/`find_library` fallbacks when pkg-config is absent, plus `-Werror` |

Remove the pump.fun-specific code while copying (curve, lighthouse, the amm and swap builders, and the exit FSM).

## Layout (`services/jito-arb/`, the spec's §12 tree with `sol-arb/` → `services/jito-arb/`)

```
CMakeLists.txt  CMakePresets.json  README.md  RUNBOOK-VM.md  config/{bot.example.toml,dex_programs.toml,denylist.json}
proto/ (copied Yellowstone protos, pinned like pump-desk's)
src/core feed state dex engine flash tx submit risk obs tools  main.cpp
tests/unit  tests/fixtures  tests/golden_tx  tests/bench (chrono harness, no Google Benchmark)
program/executor/  (Rust crate, unbuilt)
harness/  sim-parity (C++ tool) + parity-node/ (SDK harness for the VM; package.json pins versions)
```
Libraries the spec lists but are not installed get local stand-ins: spdlog → a small async logger, prometheus-cpp → a hand-written `/metrics` text endpoint, concurrentqueue → the copied ring plus an SPSC queue, tomlplusplus → the copied TOML parser. Each stand-in is noted in the README.

## Phase 0: scaffolding

- Copy the core files, CMake, config, logger, a metrics HTTP endpoint on `127.0.0.1:9100`, `Error` + `std::expected`, a `u256` helper (mul/div/shift as Whirlpool's `U256Muldiv` needs), and compact-u16.
- `main.cpp`: `jito-arb --config … [--dry-run] [--mode live|recorder|replay]`. `dry_run` is forced on unless the config says `false`. Wire the KILL file.
- **Accept:** base58, compact-u16 and u256 known-vector tests pass. `jito-arb --dry-run` loads config and serves `/metrics`. The build is clean with `-Wall -Wextra -Werror`.
- Commit: `feat(jito-arb): phase 0 scaffolding`.

## Phase 1: feed, decoders, discovery, recorder (Raydium AMM v4 and Orca Whirlpool)

- **VERIFY first:** fetch the program source and IDL layouts (raydium-amm `state.rs`, orca whirlpools `state/*.rs`, spl-token and Token-2022 extension layouts). Pin program IDs in `config/dex_programs.toml` with the source commit hash in a comment. Confirm every program ID by `getAccountInfo` (executable and owner).
- `state/`: `PoolState` variant (RaydiumV4, Whirlpool, TickArray, TokenAccount, Mint with Token-2022 extensions, Kamino Reserve stub). Discriminator and length checks. `StateStore` single writer, an `atomic<shared_ptr<const PoolState>>` per pool, `slot`/`write_version`/`seq`, an SPSC queue of changed pool ids, and quotability under `max_state_age_slots`.
- `feed/`: subscription manager (shards of about 300 pubkeys, 3–4 connections plus 1 for wallet and reserves, a slots stream), slot clock, reconnect and resync via `getMultipleAccounts` in batches of 100. Replay from the last slot only if VERIFY confirms it.
- `tools/discover`: getProgramAccounts with dataSize and memcmp filters on base mints, token graph, mints in at least 2 pools, TVL in SOL, `min_pool_tvl_sol`, `max_tracked_pools`, a subscription diff, and the token-safety gate (phase-1 version of `risk/token_safety`). It runs against a Helius key on the VM. The public RPC blocks gPA for these programs, so laptop tests use recorded gPA responses.
- `tools/recorder` and the `replay` source: append raw `SubscribeUpdate` frames with receive timestamps in a length-prefixed file, rotated hourly. Replay feeds the same StateStore.
- **Fixtures** (`tools/fetch_fixtures`, RPC): about 50 Raydium v4 pools and about 50 Whirlpools (top SOL and USDC pairs, found by known pool lists or by reading token-account owners), with their vaults, tick arrays, mints (including one Token-2022 mint with a transfer fee) and a Kamino SOL and USDC reserve. Store them as base64 plus slot in `tests/fixtures/*.json`, kept small (<5 MB total).
- **Accept (offline):** decoders round-trip every fixture. Field checks against values from an independent decode: web3.js/spl-token from pump-desk's node_modules for token accounts and mints, and a hand-checked layout via `@coral-xyz/anchor` BorshAccountsCoder using the IDL JSON fetched from GitHub for Whirlpool. Frame parse and recorder round-trip work on synthetic frames. The discovery graph and safety filter work on recorded gPA fixtures. The 24 h subscription hold and 3-day recording are deferred to the VM runbook.
- Commit(s): `feat(jito-arb): phase 1 …`.

## Phase 2: quoters, engine, replay (no sending)

- **VERIFY:** port the fee and rounding expressions verbatim from program source: Raydium v4 `swap_base_in` (trade fee numerator/denominator, pnl/need-take exclusions from vault amounts), and Whirlpool `swap_manager`/`swap_math`/`tick_math`/`bit_math` (U256 muldiv, protocol fee, fee growth). Cite the file and commit in comments.
- `dex/`: `IQuoter` as in spec §5, `raydium_v4`, `whirlpool` (tick traversal across cached arrays, `crosses_boundary` when the cache runs out), CU estimates from config, and a Token-2022 transfer-fee helper (epoch-aware).
- **Simulation parity oracle** (`harness/sim-parity`, replaces the missing SDKs on the laptop): for each fixture pool, at 20 sizes, build a direct DEX swap tx from a funded mainnet wallet that already holds both tokens (found via `getTokenLargestAccounts`) and send `simulateTransaction` with `sigVerify:false`, `replaceRecentBlockhash:true` and `accounts` returning the output token account. Take `getMultipleAccounts` before and after, and keep the sample only when the pool, vaults and tick arrays are byte-identical across the bracket. Compare `amount_out` exactly. This also proves the direct swap ixs' account order, which phase 3 reuses. Aim for at least 200 kept samples per DEX, rate-limited, and store the results in `tests/fixtures/parity_*.json` so the unit test replays them offline.
- `harness/parity-node/`: scripts using the official Raydium, Orca and Meteora SDKs with versions pinned in package.json (not installed here), emitting expected quotes as JSON over recorded snapshots. This is the full 1,000 × 20 run on the VM.
- `engine/`: route graph and cycle index (2- and 3-hop, `max_cycles_per_pool`), fixed-point prefilter, sizer (closed form for 2-hop CP, integer golden-section with at most 24 evaluations, retry at 70% on boundary), cost model, tip policy (alpha controller per route class, tip-floor input), and a dispatcher (EV heap, pool-conflict skip, `max_bundles_per_slot`, cooldown, `max_send_lag_slots`). `flash/kamino`: reserve decode and `calculate_flash_loan_fees` ported from klend source, with `u64::MAX` meaning disabled. Phase 2 also needs the reserve's available liquidity for `x_max`.
- `obs/decision_log`: a binary record per candidate as in §11. `tools/replay_report`: detections per day, size, gross, estimated tip to win, net EV. The comparison with arbs that landed on chain is a VM step that needs block data.
- `tests/bench`: chrono microbenchmarks for CP quote (<100 ns), CLMM quote (<2 µs at ≤5 ticks), and update→candidate (<200 µs), on the M-series laptop. Report the numbers. Production numbers are a VM step.
- **Accept (offline):** 100% exact match on all kept sim-parity samples for both DEXes. Sizer and engine unit tests on synthetic and fixture states (a known profitable 2-hop CP cycle finds the closed-form optimum; dispatcher conflict, cooldown and lag rules). Replay of a synthetic frame file produces a decision log and report. Benchmarks are reported. If any parity mismatch remains, keep the DEX disabled in config and say so in the PR. Never weaken the test.
- Commit(s): `feat(jito-arb): phase 2 …`.

## Phase 3: executor program, tx builder, submitter (built and tested offline only)

- **VERIFY:** Kamino flash ix accounts and data (klend `handler_flash_borrow/repay`, top-level and CPI rules, the sysvar instructions account), Jito `sendBundle` encoding, `getBundleStatuses`/`getInflightBundleStatuses`, tip-floor endpoint, rate limits, whether tip accounts may sit in an ALT (default: static key), and the ComputeBudget ix encodings. Put findings in the README's VERIFY table with sources.
- `tx/`: instruction builders (ComputeBudget, Kamino borrow/repay with the `borrow_instruction_index` computed from final position, executor `execute_route` data packed per §8, System tip transfer), the ALT-aware `message_v0` compiler (signers and the tip account stay static), the 1,232-byte check returning `tx_too_large`, the signer, `alt_manager` (create/extend ix builders, `pending_alt` until activation slot + 2, decode of ALT accounts), and a blockhash cache (refresh 2 s, refuse if older than 60 slots).
- **Golden txs:** `tests/golden_tx/gen.mjs` runs with `NODE_PATH=../../pump-desk/tools/fixtures/node_modules` (web3.js 1.99, already on disk) and builds v0 messages with one and two `AddressLookupTableAccount`s from fixture ALTs, covering the full §7.3 layout. The C++ output must be byte-identical. Commit the generated JSON so tests run without node.
- **Sim checks via RPC** (no deploy needed): (a) Kamino borrow → (own-ATA no-op) → repay from a wallet holding wSOL/USDC, using `simulateTransaction sigVerify:false`, which proves the account order and fee. (b) The same around a direct Raydium v4 swap pair, without the executor. (c) The whole §7.3 tx with a placeholder executor ID is expected to fail at the executor ix only. Record the logs in fixtures.
- `risk/`: `sign_guard` (program allowlist; exactly one System transfer, to a cached tip account, ≤ `tip_max`), limits and kill switch (§10 table), denylist with 24 h expiry, token safety in full.
- `submit/`: Jito client (fan-out to `frankfurt` and `amsterdam` over warm keep-alive conns, 429 → 1 s regional backoff, no retry after the slot passes), tip-account cache, bundle tracker (500 ms polls up to 30 s; outcomes `landed`/`not_landed`/`rejected`/`simulation_failed` fed back to the tip controller and `p_land`), and a Sender client (disabled by default). **`dry_run` path:** build, sign-guard, optionally simulate, log, never call `sendBundle`. A unit test asserts that `sendBundle` is unreachable when `dry_run=true` (the client is constructed without a sender).
- `program/executor/` (Rust, native `solana-program`, no Anchor, `msg!` only behind a feature): parse `ExecuteRouteArgs`, owner check against a hard-coded pubkey constant, pre/post balance measurement, a per-leg CPI dispatch for RaydiumV4 and Whirlpool (other DEX enum values return `Unsupported`), and the custom `min_out` error. Include Rust unit tests for arg parsing and the guard math, plus `tests/validator.sh` for `solana-test-validator --clone …` with the profitable route and a tampered `min_out`. **Not compiled here.** The PR and runbook say so plainly. C++ has a test that `execute_route` data bytes match the Rust struct layout (a fixture shared by both).
- **Accept (offline):** golden txs byte-identical; sign-guard and limit tests; Kamino and swap sim checks pass on mainnet RPC; dry-run end to end on replayed frames produces signed bundles in the decision log with zero network sends. Deferred to the VM: compile and validator-test the executor, ALT creation on devnet, Kamino around the executor on a local validator.
- Commit(s): `feat(jito-arb): phase 3 …`.

## Wrap-up

- `README.md`: what is built, what is verified offline, a VERIFY table with sources, and a deviations list (local stand-ins for spec libraries, C++23 for `std::expected`, the sim-parity oracle).
- `RUNBOOK-VM.md`, an ordered list of the VM steps: install grpc++/Rust/Solana CLI/npm SDKs, build with `ARB_WITH_GRPC=ON`, run discovery with a Helius key, the 24 h subscription hold, recording for 3 days (VM disk, not laptop), the full SDK parity, the phase-2 replay gate (≥20 detections/day above `min_ev_lamports` after a realistic tip), then the executor build and validator tests. **No mainnet sends before the owner reviews the gate.**
- Push `feat/jito-arb` and open a PR against main whose body summarises the per-phase acceptance, listing passed and deferred items separately. After opening it, use the ccd_pr tools (get_status, bind_pr).
- Save a memory file `jito-arb-branch.md` and add it to MEMORY.md.

## Guardrails for the unattended run

- Never touch `pump-desk/` or any live service. Copy files out of them only.
- No private keys anywhere. Sim checks use `sigVerify:false` with other people's public wallets as fee payers and never sign with a real key. Tests use `Keypair::from_seed` with fixed test seeds.
- No `sendBundle`, `sendTransaction` or any write RPC. Only reads and `simulateTransaction`.
- Keep the RPC load small: at most about 5 req/s on the public RPC, with backoff on 429, and fixtures capped at about 5 MB.
- If a VERIFY item cannot be resolved from official source, leave the feature disabled behind config, record it in the README's VERIFY table, and continue.

## Verification (end of run)

```
cmake --preset release && cmake --build --preset release
ctest --preset release            # unit, decoder fixtures, parity replay, golden tx, risk, dry-run e2e
./build/release/arb_bench          # latency numbers into the PR
./build/release/jito-arb --config config/bot.example.toml --mode replay --replay tests/fixtures/frames.bin --dry-run
```
Plus the live, read-only RPC tools, run once with their outputs committed: `sim-parity` (both DEXes) and `sim-check` (Kamino and swap).
