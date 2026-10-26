# pk-maker-cpp (`pkm`)

The live Kalshi-priced maker on Polymarket's 15-minute crypto Up/Down markets,
as a lean C++20 process. It replaces the Rust `pk-polymaker` with the same rule
and defaults. It has no tape, journal, metrics, status page or shadow mode: the
wallet is the record. Its log is stderr (journald).

## Why a maker, and why this process

- **Why a maker.** Polymarket holds every taker order about 210 ms, and its
  makers pull the good trades in that time:
  - epoch 12 lost −$24.31 over 43 live trades;
  - live entries took ~225 ms from decision to acknowledgement, against ~43 ms
    for post-only orders on the same connection;
  - resting post-only orders and cancels are not held.

  So the strategy quotes. The 4.5-day tape replay of the rule made
  +1.00¢ ± 0.41 per filled share at h = 0.02.
- **Why this process.** Kalshi leads Polymarket by only 0–40 ms, which is how
  long a resting bid has to be pulled once Kalshi moves against it. The Rust
  maker re-quoted on a 50 ms timer and read fills from a 1 s poll. `pkm`
  differs in three ways:
  - it re-quotes on every book update;
  - it sends a frame's cancels from that frame;
  - it reads fills from Polymarket's user channel.

## The rule (pk-polymaker's, unchanged in value)

| | Default (`PK_PM_*`) |
|---|---|
| Fair q = Kalshi YES mid, when its spread is ≤ 0.02 and the book is ≤ 10 s old | `MAX_KALSHI_SPREAD`, `KALSHI_MAX_AGE_MS` |
| Buy Up at `floor(q − h − skew)`, Down at `floor(1 − (q + h − skew))`, 1¢ ticks, post-only GTC, each a tick under its best ask, within 0.05–0.95 | `HALF_SPREAD` 0.02 |
| `skew = k · net / max_net`, net = Up − Down shares this window | `SKEW_K` 0.01, `MAX_NET` 20 |
| First legs only in the first 5 min and while q is in 0.15–0.85 | `OPEN_LEG_S`, `OPEN_MIN/MAX_FAIR` |
| Pair guard: the other side pays at most `1 − c − 0.02` | `PAIR_MARGIN` |
| 5 shares a bid; open cost ≤ $60 | `SHARES`, `MAX_OPEN_COST_USD` |
| A 2¢ Kalshi move within 1 s pulls both bids for 1.5 s | `PAUSE_JUMP`, `PAUSE_MS` |
| Not quoted in the first 20 s or the last 60 s | `ENTRY_EARLY_S`, `STOP_S` |
| A bid above its target goes after 200 ms; one ≥ 1¢ below is raised after 300 ms | `ADVERSE_MS`, `MIN_REQUOTE_MS` |
| ≤ 8 posts/s; a refused post cools 500 ms; a "not enough balance" refusal holds bids the free balance cannot cover for 3 s | `MAX_ORDERS_PER_S` |
| Realised −$15 today latches a halt (persisted) until the next UTC day or the `ACK` file | `DAILY_LOSS_USD` |
| Cancel-all at start, on halt, on stop, on the `KILL` file, and when the hot loop stalls 2 s (dead man) | |

The rule lives in `src/engine.cpp`. Its tests in `tests/test_engine.cpp` are
pk-polymaker's own, ported.

## Architecture

```
core 2: hot thread (busy-polls)            core 3: signer thread
  Kalshi WS ─┐                               keeps each slot's target
  Poly market WS × coin ─┼─ parse → book → engine     and ±1 tick signed
  Poly user WS (fills) ─┘        │                    (seqlocks, one use each)
  4 warm HTTP/1.1 order connections ◀── posts / cancels sent from the frame
core 1: connector · control · redeemer threads
  handshakes handed over · discovery (Gamma, Kalshi) · fill backstop poll ·
  wallet and orphan reconciliation · settlement · relayer redemption ·
  KILL/ACK files · dead man · state.json
```

| | Measured (`pkm-bench`, this repo's CI box) |
|---|---|
| Kalshi delta → book → engine, nothing to send | ~1 µs p50, no allocation |
| Kalshi jump → two signed DELETE requests ready | ~3 µs p50, no allocation |
| Post from a pre-signed body → request ready | ~2 µs p50, no allocation |
| Post signed inline (a pre-sign miss) | ~35 µs |

The network round trip (~43 ms Dublin → the CLOB) is all that is left.

**Venue protocol, ported exactly.**
- **Orders:** CLOB V2 order struct, EIP-712 domain "Polymarket CTF Exchange" v2.
  The deposit wallet's ERC-7739 `TypedDataSign` wrapper is copied from
  `polymarket_client_sdk_v2` 0.8.0.
- **Auth:** L1 `ClobAuth`, then L2 HMAC headers.
- **Redemption:** the relayer `Batch` to the CTF collateral adapter.
- **Checks:** all of the above match golden vectors byte for byte.
  `poly-kalshi-service/crates/pk-exec/tests/clob_vectors.rs` writes them from
  the SDK's own types into `tests/fixtures/clob_vectors.txt`.
- **Version guard:** `pkm` refuses to run when the CLOB reports any protocol
  version other than 2.

**Wallet.** A deposit wallet (`POLY_SIGNATURE_TYPE=3`) only:
- it is the account's funded wallet, with the approvals already in place;
- redemption is gasless through the relayer;
- an EOA would sign no faster on the hot path, since signing is done ahead.

## Build and test

```sh
sudo apt install build-essential cmake ninja-build libssl-dev libsecp256k1-dev
cmake --preset release && cmake --build --preset release && ctest --preset release
cmake --preset sanitizers && cmake --build --preset sanitizers && ctest --preset sanitizers
build/release/pkm-bench
```

Without the system libsecp256k1 the build fetches v0.6.0 (as hyperliquid-arb
does). OpenSSL ≥ 3.0. There is no other dependency: the WebSocket, HTTP/1.1,
JSON and keccak code is in `src/`.

| Test | What it covers |
|---|---|
| `pkm-tests` | crypto vectors, JSON, transport against local TLS servers, books, feed decoders, the engine |
| `pkm-e2e` | `pkm run`'s runtime against an in-process mock of every venue route: discovery, both feeds, signed bids at the rule's prices, cancels on a Kalshi jump, requotes after the pause, a user-channel fill booked, cancel-all on stop, the state file |
| `pkm-bench` | the frame-to-wire timings above, and that the path does not allocate |

## Run

```sh
pkm --env /etc/pkm.env check        # auth, V2, headers, cash, one post-only probe far under the bid, cancelled
pkm --env /etc/pkm.env cancel-all
pkm --env /etc/pkm.env run          # needs PKM_LIVE_CONFIRM=yes
```

Settings are in `config.example.env`. Deployment, the kill switch, the halt
ACK and the cutover from pk-polymaker are in `RUNBOOK.md`.
