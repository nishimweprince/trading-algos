# pump-desk

A C++ rewrite of `pump-fun/` that trades the **migration racer** strategy live. It watches pump.fun bonding curves that are nearly complete. When one graduates, it buys first position on the new canonical PumpSwap pool and exits it with a configurable rule set.

> **Live only.** There is no dry-run or paper mode; every trigger sends a real transaction from `WALLET_PRIVATE_KEY`. The racer's own replay of the current exit set was −4.7 %/trade (CI −11.9..+2.5, `pump-fun/config.yaml`), so the first trades are the measurement. Start with a small wallet.

## What it does

1. **Admit.** It streams every curve with fewer than `band_values · 2^40` raw tokens left over LaserStream (Yellowstone gRPC). A curve is tracked once its completion cost is ≤ `racer.admit_cost_sol`. The mint is known from the create tx (also streamed). Curves created before boot are resolved by an aux RPC.
2. **Fire.** At completion cost ≤ `racer.max_completion_cost_sol` (0.5 SOL), it derives every key of the canonical pool the migration will create and starts watching the pool and both vaults:
   - pool authority, pool, vault ATAs, user ATA
   - coin-creator vault, pool-v2
3. **Trigger.** Landed migrate tx, pool-account push and vault pushes can arrive in any order. It fires once the pool account (the true `virtual_quote_reserves`, which varies 0–17.6 SOL by pool) and both vault balances are known. If the migrate tx arrives but the pool push doesn't follow within `pool_push_wait_ms`, it prices off the default.
4. **Screen** (pure CPU, all inputs prepared at admission):
   - kill switch and PumpSwap buy flag
   - blockhash age
   - blacklist and `pump` suffix
   - Token-2022 transfer fee: proven absent from the create logs or a mint read, else vetoed (fail closed)
   - real pool SOL within `[25, 100]`
   - buy impact ≤ 3 %
   - concurrency and balance
5. **Buy.** Size is 15 % of the available balance (compounding). It builds the PumpSwap `buy` exactly as `@pump-fun/pump-swap-sdk` 1.20 does: WSOL wrap → base ATA → buy → unwrap, with CU price and a Sender tip. It signs with libsodium and hands the packet to the exec thread.
6. **Fills** come from the same gRPC stream (a wallet transaction filter). Unconfirmed orders are resolved by RPC.
7. **Exits** are a port of `pump-fun/src/exits/engine.ts` `evaluateExit`: TP0/TP1/TP2, hard stop, trailing, time stop, dead money and slippage tiers. Defaults are the live set: TP 50 % full exit, no stop, trailing off, 10-minute time stop. A full exit also closes the base ATA, which returns its rent.

## Threads

| CPU | Thread | Work |
|---|---|---|
| 1 | ingest | grpc++ generic bidi stream. Raw `SubscribeUpdate` bytes go into a preallocated frame ring (Disruptor-style; logic gates it, the capture recorder reads lossily). Owns subscription rewrites and pings. |
| 2 | logic | Zero-copy protobuf wire parse, then tracker, trigger, screen, sizing, tx build and sign, positions, exits. No heap after construction. |
| 3 | exec | Spins on the packet queue. Writes to warm keep-alive HTTP/1.1 sockets (`TCP_NODELAY`) to Helius Sender fra, and optionally Jito Frankfurt with the same signed bytes. Reads acks between packets. |
| any | aux-fast, aux-req, writer, capture | Blockhash (2 s), PumpSwap configs (30 s), priority fee (3 s), balance, KILL file; curve→mint and unconfirmed-order RPC; SQLite + JSONL. |

Measured in-process trigger latency (frame in → signed packet queued, `bench_trigger_latency`): **~20–30 µs p50** on an M-series laptop, unpinned.

## Correctness of the hand-rolled bytes

- `tools/fixtures/gen.mjs` builds 10 buy/sell variants with the official SDK and web3.js, using real mainnet accounts, and writes `tests/fixtures/golden.json`. The variants cover: coin creator default, cashback, mayhem, creator-fee override, no virtual reserve, and sells. `pd_tests` asserts the compiled messages are **byte-identical**, along with 64 PDA/ATA vectors and 256 on-curve vectors.
- Boot runs a **self-check**: it builds a buy on a graduated canonical pool and `simulateTransaction`s it. Any program error refuses the start.
- Before this commit, the C++ buy was simulated OK against mainnet on the seed pool and on 4 fresh migrations (2 of them mayhem pools).

Regenerate fixtures after an SDK upgrade with `node pump-desk/tools/fixtures/gen.mjs` (needs `pump-fun/node_modules`).

## Build and deploy (AWS eu-central-1)

Build only on the VM; grpc++ is not installed on the laptop.

1. Launch Ubuntu 24.04 in **eu-central-1**: c7i.2xlarge or larger, 4+ physical cores. Use ENA and an elastic IP.
2. Clone the repo, check out `feat/pump-desk-cpp`, then run `bash pump-desk/deploy/aws/bootstrap.sh`. It does: apt deps → sysctl → chrony on the AWS time source → Release build (`-O3 -march=native -flto`) → `pd_tests` → systemd units for the trader and the status page.
3. Fill `pump-desk/.env` (mode 600): `HELIUS_GRPC_URL` (LaserStream Frankfurt), `HELIUS_GRPC_TOKEN`, `HELIUS_HTTP_URL`, `WALLET_PRIVATE_KEY` (base58 or JSON array). Optional: `SENDER_URL`, `JITO_URL`.
4. Review `pump-desk/pump-desk.toml`, which starts as a copy of `config.example.toml`.
5. **Cores**: 4 physical cores. On c7i, logical cpu N and N+4 are two threads of one physical core. Isolate the three hot cores and their siblings so the kernel, the writer, ssh, cloudflared, and the status page stay on the remaining core (cpus 0 and 4):

   `isolcpus=1-3,5-7 nohz_full=1-3,5-7 rcu_nocbs=1-3,5-7`

   Add that to `GRUB_CMDLINE_LINUX_DEFAULT` in `/etc/default/grub`, run `sudo update-grub`, then reboot. Check `lscpu -e`: cpus 1, 2, and 3 must be different physical cores from each other and from cpu 0.
6. Preflight (no subscription, no trades): `set -a; . .env; set +a; ./build/pump-desk --config pump-desk.toml --preflight`
7. Go live: `sudo systemctl enable --now pump-desk`, then `journalctl -u pump-desk -f`.

## Operating

- **Stop entries:** `touch pump-desk/KILL`. Exits keep running. `rm` it to resume.
- **Stop the process:** `sudo systemctl stop pump-desk`. Open and in-flight positions are persisted. On the next start they are restored from `data/pump-desk.db` after checking the wallet's token balance, and re-marked from the pool.
- **Kill latch:** if an exit fails `max_exit_attempts` times, entries stop and that position is left for you to sell by hand.
- **Sender 429s:** exec resends the same signed bytes on the other link after `sender.retry_429_ms` × try, up to `sender.max_429_retries`. A 429 is never a rejection, so it costs no exit attempt and can't trip the kill latch. A sell rejected at send time (no connection, a rejected request) waits `exits.exit_retry_ms` before the next attempt. Only an on-chain failure retries at once on the next slippage tier.
- **Landing, not "fill ms":** sent → fill seen (~150 ms) is mostly chain and stream time. Judge routing and fee changes on the landing fields of each buy fill: slots to land (fill slot − processed tip at send), index in the block, pool quote-vault writes by other txs between our send and ours, and first shred / slot completed → seen (from LaserStream inter-slot statuses; n/a if the endpoint doesn't send them). They're in the `fills` view, the report, and the status page.
- **Report:** `./build/pump-desk-report data/pump-desk.db [--since-hours 24]` shows net PnL including fees, tips and rent, exit reasons, landing and latency percentiles. Raw data is in the `events` table and the `decisions`, `orders`, `acks`, `fills` and `migrations` views, plus `data/events.jsonl`.
- **Status page:** `sudo systemctl enable --now pump-desk-status` serves `http://127.0.0.1:8787`. The page polls `/api/status` every 10 seconds. The process only reads `data/pump-desk.db` and is left unpinned, so with the isolcpus line above it stays on the housekeeping core. Do not publish port 8787. Point cloudflared at that URL:

  `cloudflared tunnel --url http://127.0.0.1:8787`

  A named Cloudflare tunnel uses the same origin when you want a stable hostname. Stream, frames, reconnects, parse errors, and drops come from the stats row (since process start). Fires, decisions, vetoes, fills, and PnL are the selected window, default 24 hours (`/api/status?hours=24`).
- **Status line** every 10 s in the journal: stream state, frames, fires, triggers, vetoes, positions, wallet.

## Layout

```
src/core     keys, base58/64, protobuf wire, rings (SPSC, frame ring, seqlock), config, clocks/pinning
src/sol      constants, PDAs + on-curve, layouts, PumpSwap fee/quote math, v0 compiler, swap builders
src/stream   SubscribeUpdate views, SubscribeRequest encoder, grpc++ ingest
src/logic    engine (tracker/trigger/screen/positions), tx parsing, exit FSM, shared queues/events
src/exec     Sender/Jito sender
src/net      TCP/TLS connections, HTTP/1.1 parser
src/aux      RPC client, aux threads, writer
src/tools    report, status page
tests/       golden SDK bytes, engine round trips over synthetic frames, rings, parser, writer
proto/       vendored LaserStream geyser.proto / solana-storage.proto (field reference; no protoc needed)
```

## Not done / known gaps

- The gRPC ingest (`src/stream/ingest.cpp`) compiles only on the VM. Everything else builds and is tested locally.
- Jito is built but off. Enable `jito.enabled` only after `pump-desk-report` shows the extra tip ix costs nothing measurable. The send itself is parallel.
- Exits rely on vault pushes (every swap writes both vaults) plus a timer. No RPC polling on positions.
- No shred-level feed. A Jito ShredStream proxy in FRA would see migrate txs and our fills from raw block data before replay. The report's first shred → seen and slot completed → seen numbers size the possible gain. It needs a Jito auth key and a separate process.
- No `migrate` instruction and no creation sniping, by design: the migrate caller earns nothing (`pump-fun/reports/migrate-racer-verdict.md`).
