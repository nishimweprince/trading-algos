# pump-desk

> **Status (2026-10-21): curve-in, migrate-out, live as a capped experiment.**
> The migration racer is gone. The strategy's own replay failed its gate (test
> mean −12.9 %/trade, CI −20.9..−5.9; `reports/curve-in-migrate-out.md`): the
> sell into the first post-migrate slots averages +3.6 %, but buys that land
> after completion (−2 %) and curves that collapse before completing (−66 %)
> outweigh it. The owner chose to run it live anyway, at 0.05 SOL per trade with
> `risk.max_loss_sol = 0.06` latching the kill. Judge it on `pump-desk-report`.

A live C++ trader on pump.fun bonding curves. It buys the last part of a nearly complete curve, holds through completion and the migrate, and sells on the canonical PumpSwap pool into the racers that buy in the first slots after the migrate.

> **Live only.** There is no dry-run or paper mode; every entry sends a real transaction from `WALLET_PRIVATE_KEY`.

## What it does

1. **Admit.** It streams every curve with fewer than `band_values · 2^40` raw tokens left over LaserStream (Yellowstone gRPC). A curve is tracked once its completion cost is ≤ `curve.admit_cost_sol` (5 SOL). The mint is known from the create tx (also streamed); curves created before boot are resolved by an aux RPC. Once the mint and its token program are known it derives the curve's keys (bonding curve, its ATA, `bonding-curve-v2`, creator vault, user ATA and volume accumulator) and the canonical pool's keys, off the hot path.
2. **Screen** on every curve update inside the entry range, `entry_min_remaining_sol < cost ≤ entry_max_remaining_sol` (default (0, 4] SOL):
   - kill switch, the max-loss latch, config and blockhash present
   - blacklist and `pump` suffix
   - SOL-quoted curves only (quote-chain curves are skipped), mayhem curves skipped unless `allow_mayhem`
   - Token-2022 transfer fee proven absent, else vetoed (fail closed)
   - **overhang** (off by default): the price drop if the top `curve.overhang_top_k` holders sold everything on the curve, `1 − (vtok / (vtok + H))²`. H is read by aux-req (`getTokenLargestAccounts`, minus the curve's own account and ours) every `overhang_refresh_ms` while a curve is admitted and untraded. With `overhang_max_drop_pct > 0`, a drop above it is a soft veto (`overhang`), as is a read older than `overhang_max_age_ms` (`overhang_unknown`). The drop, holder and creator tokens are logged on every decision row either way.
   - one position at a time (`risk.max_concurrent_positions = 1`), never twice on one curve, balance
   Soft vetoes (kill, fee still resolving, balance) retry on the next update and log one decision row per change.
3. **Buy** `entry.size_sol` (0.05 SOL) on the curve with pump.fun `buy`, built exactly as `@pump-fun/pump-sdk` 4.2.0 does, with `partial_fill` set: a buy that meets fewer tokens than it asked for fills what is left (completing the curve) instead of failing 6021. `max_sol_cost` = the quoted cost × (1 + `entry.slippage_pct`). Native SOL, no WSOL wrap. It watches the pool and both vaults the migrate will create.
4. **Curve phase.** Marks come from the curve account. `CURVE_STALL`: not complete `curve.max_hold_s` (30 s) after the fill → sell back on the curve. `CURVE_ABORT` (off by default): completion cost climbs past `abort_remaining_sol`.
5. **Await migrate.** Once the curve is complete nothing can trade. If no migrate arrives within `curve.migrate_wait_s`, the exit goes to Jupiter (`AWAIT_TIMEOUT`).
6. **Pool phase.** The migrate tx (or the pool push, whichever is first) moves the position to the pool, priced off the migrate's vault balances and the pool's virtual quote reserve. `POOL_TP` once the mark is `exits.tp_pct` over entry (off by default); else `POOL_TIMEOUT` `exits.timeout_slots` (3) after the migrate slot. The PumpSwap sell also closes the base ATA (rent back).
7. **Fills** come from the same gRPC stream (a wallet transaction filter); pump.fun errors 6005/6081 (curve complete) and 6002/6003 (slippage) are recognised. Unconfirmed orders are resolved by RPC. A curve sell that meets a completed curve is not retried: the position waits for the migrate.
8. **Risk.** Realized loss since boot ≥ `risk.max_loss_sol` latches the kill (no new entries; exits run).

## Routing

Every order is priced for the leader of the next slot (`[routing.<class>]`, default `estimate`: the Helius priority-fee estimate over the pump.fun and PumpSwap programs × `entry.priority_multiplier`; sells × `exits.exit_priority_multiplier`, 1 by default). `floor` and `beat_rivals` remain available.

- **Leader table.** `aux-leaders` loads `getLeaderSchedule` (current and next epoch) and `getClusterNodes` (every 10 minutes). It classifies each identity: on the BAM list (`data/bam_validators.json`, a JSON array you maintain) → BAM; version `0.x` → Firedancer; else Agave.
- **CU limit.** Buys use `entry.cu_limit` until `routing.cu_samples_min` buys have landed, then the max of the last 32 + 15 %. Sells use `exits.cu_limit`. Measured on mainnet: curve buy ~73k CU, curve sell ~52k CU.
- **Jito per packet.** Exec sends to Jito only packets priced for it. The class's `jito = true` needs `jito.enabled`.

### Who we sell into

From each buy, every tx on the pool is streamed for `rival_window_slots` slots after the migrate (filter `pools`) into `rival_txs` rows: signer, slot and index, CU price/limit, tip and route, SOL in (+ buy, − sell), leader class. The report ranks the wallets buying and selling our pools after the migrate.

## Threads

| CPU | Thread | Work |
|---|---|---|
| 1 | ingest | grpc++ generic bidi stream. Raw `SubscribeUpdate` bytes go into a preallocated frame ring (Disruptor-style; logic gates it, the capture recorder reads lossily). Owns subscription rewrites and pings. |
| 2 | logic | Zero-copy protobuf wire parse, then tracker, trigger, screen, sizing, tx build and sign, positions, exits. No heap after construction. |
| 3 | exec | Spins on the packet queue. Writes to warm keep-alive HTTP/1.1 sockets (`TCP_NODELAY`) to Helius Sender fra, and optionally Jito Frankfurt with the same signed bytes. Reads acks between packets. |
| any | aux-fast, aux-req, aux-leaders, writer, capture | Blockhash (2 s), PumpSwap configs (30 s), priority fee and route profiles (3 s), rivals file, balance, KILL file; curve→mint and unconfirmed-order RPC; leader schedule and cluster versions; SQLite + JSONL. |

Measured in-process entry latency (curve frame in → signed curve buy queued, `bench_entry_latency`): **~15 µs p50** on an M-series laptop, unpinned. A curve that jumps straight into the entry range without having been admitted first also pays for its key derivation (~1.5 ms).

## Correctness of the hand-rolled bytes

- `tools/fixtures/gen.mjs` builds 10 buy/sell variants with the official SDK and web3.js, using real mainnet accounts, and writes `tests/fixtures/golden.json`. The variants cover: coin creator default, cashback, mayhem, creator-fee override, no virtual reserve, and sells. `pd_tests` asserts the compiled messages are **byte-identical**, along with 64 PDA/ATA vectors and 256 on-curve vectors.
- `tools/fixtures/gen_curve.mjs` builds pump.fun curve buys (partial fill, exact, mayhem) and sells (plain, cashback) with the official `@pump-fun/pump-sdk` 4.2.0 (pinned in `tools/fixtures/package.json`) against a live curve and the pump.fun `Global`, and writes `tests/fixtures/curve_golden.json`. `pd_tests` asserts byte-identical messages, the PDAs, and the curve quotes against the SDK's.
- Boot runs a **self-check**: a PumpSwap buy on a graduated canonical pool must simulate OK, and a curve buy on that mint's (completed) curve must reach pump.fun's curve-complete check (6005/6081): every account passed the program's constraints. Anything else refuses the start.
- Before this commit, the C++ curve buy (partial fill) and sell were simulated OK against mainnet as a real holder of a live curve (73k / 52k CU), and the SDK's partial-fill buy filled the remaining tokens where the exact buy failed 6021.
- The Lighthouse builders (`src/sol/lighthouse.*`, `tools/fixtures/lighthouse.mjs`) stay as tested library code; no order uses them.

Regenerate fixtures after an SDK upgrade with `node pump-desk/tools/fixtures/gen.mjs` (needs `pump-fun/node_modules`) and, after `(cd pump-desk/tools/fixtures && npm install --ignore-scripts)`, `node pump-desk/tools/fixtures/gen_curve.mjs [RPC] [LIVE_CURVE_MINT]` (the mint's curve must still be incomplete).

## Build and deploy (AWS eu-central-1)

Build only on the VM; grpc++ is not installed on the laptop.

1. Launch Ubuntu 24.04 in **eu-central-1**: c7i.2xlarge or larger, 4+ physical cores. Use ENA and an elastic IP.
2. Clone the repo, then run `bash pump-desk/deploy/aws/bootstrap.sh`. It does: apt deps → sysctl → chrony on the AWS time source → Release build (`-O3 -march=native -flto`) → `pd_tests` → systemd units for the trader and the status page.
3. Fill `pump-desk/.env` (mode 600): `HELIUS_GRPC_URL` (LaserStream Frankfurt), `HELIUS_GRPC_TOKEN`, `HELIUS_HTTP_URL`, `WALLET_PRIVATE_KEY` (base58 or JSON array). Optional: `SENDER_URL`, `JITO_URL`, `JUPITER_API_KEY` (the exit of last resort, below).
4. Review `pump-desk/pump-desk.toml`, which starts as a copy of `config.example.toml`.
5. **Cores**: 4 physical cores. On c7i, logical cpu N and N+4 are two threads of one physical core. Isolate the three hot cores and their siblings so the kernel, the writer, ssh, cloudflared, and the status page stay on the remaining core (cpus 0 and 4):

   `isolcpus=1-3,5-7 nohz_full=1-3,5-7 rcu_nocbs=1-3,5-7`

   Add that to `GRUB_CMDLINE_LINUX_DEFAULT` in `/etc/default/grub`, run `sudo update-grub`, then reboot. Check `lscpu -e`: cpus 1, 2, and 3 must be different physical cores from each other and from cpu 0.
6. Preflight (no subscription, no trades): `set -a; . .env; set +a; ./build/pump-desk --config pump-desk.toml --preflight`
7. Go live: `sudo systemctl enable --now pump-desk`, then `journalctl -u pump-desk -f`.

## Operating

- **Stop entries:** `touch pump-desk/KILL`. Exits keep running. `rm` it to resume.
- **Stop the process:** `sudo systemctl stop pump-desk`. Open and in-flight positions are persisted. On the next start they are restored from `data/pump-desk.db` after checking the wallet's token balance; the phase is read from the chain (curve incomplete → curve; pool exists → pool, sold at once since the timeout is past; else awaiting the migrate).
- **Kill latch:** if an exit fails `max_exit_attempts` times, a completed curve sees no migrate in time, or realized loss reaches `risk.max_loss_sol`, entries stop until restart.
  - **With `JUPITER_API_KEY` set,** the position is then sold through Jupiter's Swap API, the exit of last resort, routed over every pool Jupiter indexes:
    - aux-req quotes it and has Jupiter build the swap.
    - It refuses the tx unless the only signer and fee payer is our wallet, or if Jupiter's own simulation fails.
    - It signs and sends through the RPC. The stream's fill closes the position as `jupiter_exit`.
  - It makes up to `jupiter.max_attempts` attempts: a no-route answer retries after 5 s, and an unconfirmed send after `confirm_ms`.
  - After that, or without the key, the position is left for you to sell by hand.
  - Preflight logs whether the fallback is ready.
  - The base ATA's rent stays in the token account after a Jupiter exit; close it by hand to reclaim it.
- **Sender 429s:** exec resends the same signed bytes on the other link after `sender.retry_429_ms` × try (default 1 s; live, Sender let through about 1 tx/s for this key), up to `sender.max_429_retries`. A 429 on a sell also restarts that sell's exit timer, so the engine doesn't send a second sell into the backoff. A 429 is never a rejection, so it costs no exit attempt and can't trip the kill latch. A sell rejected at send time (no connection, a rejected request) waits `exits.exit_retry_ms` before the next attempt. Only an on-chain failure retries at once on the next slippage tier.
- **Landing, not "fill ms":** sent → fill seen (~150 ms) is mostly chain and stream time. Judge routing and fee changes on the landing fields of each buy fill: slots to land (fill slot − processed tip at send), index in the block, and first shred / slot completed → seen (from LaserStream inter-slot statuses). For this strategy the numbers that matter are buys landed vs `complete_before_land`, and migrate → sell landed (slots).
- **One bot per wallet.** A wallet tx that isn't ours but moves a held position's tokens out (a wallet app, another bot on the same key) is followed: the position shrinks, or closes as `external_sell`, instead of exits retrying into an emptied token account.
- **Report:** `./build/pump-desk-report data/pump-desk.db [--since-hours 24]` shows:
  - net PnL including fees, tips and rent; exit reasons; closes by phase
  - hold: buy fill → migrate, migrate → sell landed (slots)
  - landing and latency percentiles, buys by leader class
  - overhang at buy: buys bucketed by the overhang drop, with their closes' P&L
  - who buys and who sells our pools after the migrate

  Raw data is in the `events` table, the `decisions`, `orders`, `acks`, `fills`, `routes`, `rival_txs`, `migrations` and `closes` views, and `data/events.jsonl`.
- **Rivals:** `pump-desk-rivals.timer` refreshes `data/rivals.json`, used only by `beat_rivals` pricing (off by default).
- **BAM list:** put the BAM validators' identities in `data/bam_validators.json` (`["identity", ...]`). Without it, every non-Firedancer leader is treated as Agave.
- **Status page:** `sudo systemctl enable --now pump-desk-status` serves `http://127.0.0.1:8787`. The page polls `/api/status` every 10 seconds. The process only reads `data/pump-desk.db` and is left unpinned, so with the isolcpus line above it stays on the housekeeping core. Do not publish port 8787. Point cloudflared at that URL:

  `cloudflared tunnel --url http://127.0.0.1:8787`

  A named Cloudflare tunnel uses the same origin when you want a stable hostname. Stream, frames, reconnects, parse errors, and drops come from the stats row (since process start). Curves admitted, decisions, vetoes, fills, and PnL are the selected window, default 24 hours (`/api/status?hours=24`).
- **Status line** every 10 s in the journal: stream state, frames, P&L since boot, migrates, entries, vetoes, positions, wallet.

## Layout

```
src/core     keys, base58/64, protobuf wire, rings (SPSC, frame ring, seqlock), config, clocks/pinning
src/sol      constants, PDAs + on-curve, layouts, PumpSwap fee/quote math, v0 compiler, swap builders
src/stream   SubscribeUpdate views, SubscribeRequest encoder, grpc++ ingest
src/logic    engine (tracker/trigger/screen/positions), tx parsing, exit FSM, shared queues/events
src/exec     Sender/Jito sender
src/net      TCP/TLS connections, HTTP/1.1 parser
src/aux      RPC client, aux threads (fees/routes/rivals, leader schedule), writer
src/tools    report, status page
tests/       golden SDK bytes, engine round trips over synthetic frames, rings, parser, writer
proto/       vendored LaserStream geyser.proto / solana-storage.proto (field reference; no protoc needed)
```

## Not done / known gaps

- The gRPC ingest (`src/stream/ingest.cpp`) needs grpc++ (brew grpc on the laptop, the VM's build otherwise).
- **The edge is unproven** (see the status note). Selecting curves that will complete is the open problem. The overhang filter is the first collapse filter. It stays off until `research/replay_curve.py` on holder history (`fetch_curve_paths.py` schema 2) passes its gate with it on. It sees top holders only, not wallets clustered by funder.
- A held curve that sells off far enough leaves the band filter: its marks stop, and `CURVE_STALL` sells off the last curve state seen (the protective tiers widen the bound on retries).
- The max-loss latch counts losses since boot; a restart resets it.
- No Helius Preconfirmations yet: the migrate and our fills come from LaserStream at processed commitment.
- Jito is built but off.
