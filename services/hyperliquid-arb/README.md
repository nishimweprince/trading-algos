# Hyperliquid ↔ MT5 arbitrage (C++, Windows box in AWS Tokyo)

`hlarb` quotes Hyperliquid (HL) as a maker and hedges every HL fill on MT5.

- **HL is the slow leg.** Its `bbo` pushes about every 100 ms, and an order waits about 200 ms for block inclusion. That is the venue where prices go stale, and HL processes cancels before new orders inside a block, which protects resting makers. So we quote there with ALO orders.
- **MT5 is the fair price and the hedge.** It is fast, so it is never the side we trade stale prices on.

The research behind this is in [`hyperliquid-arbitrage.md`](hyperliquid-arbitrage.md) and [`research-hyperliquid.md`](research-hyperliquid.md). The exact rules are in [`STRATEGY_SPEC.md`](STRATEGY_SPEC.md), operations in [`RUNBOOK.md`](RUNBOOK.md), and the host and broker choice in [`docs/aws-tokyo-windows.md`](docs/aws-tokyo-windows.md).

## Layout

```
HL public WS (bbo, activeAssetCtx) ─ feed thread ─┐ SPSC
HL private WS (orderUpdates, userFills, post) ─ gateway thread ⇄ ┼─▶ decision thread (Engine) ─▶ recorder → journal, status
HL info (margin, positions, rate limit) ─ aux thread ─┘   │ shared memory (hla_bridge.dll)
                                                          ▼
terminal64.exe: HlaPublisher (ticks, specs, account) / HlaExecutor (OrderSend, positions)
```

| Module | What it does |
|---|---|
| `hl_sign` | msgpack, keccak, EIP-712 phantom agent, secp256k1. Byte-identical to the official SDK (`tests/fixtures/hl_sign_vectors.txt`). |
| `hl_gateway` | Signed actions over a WS `post`, with REST fallback. Parses acks, fills and order updates; refreshes the `scheduleCancel` dead-man switch. |
| `hl_instruments`, `hl_info` | HIP-3 asset ids (`xyz:JPY` → 110024), ticks, growth mode; per-dex clearinghouse, open orders, rate limit, fees, agents. |
| `bridge_layout`, `mt5_bridge`, `mt5/bridge/hla_bridge.cpp` | Lock-free shared-memory rings to the MQL5 services. Hedge orders expire in the DLL. |
| `engine` | Fair value, quotes, requotes, hedging, inventory, blocks and halts. |
| `runtime` | Preflight, threads, decision loop, dry-run simulation, shutdown sweep. |
| `drills` | 16 failure scenarios through the production engine. |

## Build and test

You need CMake ≥ 3.25, Ninja, C++20 clang, OpenSSL ≥ 3.2 (for KECCAK-256), and libcurl built with wss. libsecp256k1 v0.6.0 is fetched and built by CMake. On Windows, use MSYS2 **CLANG64**; MSVC is rejected because the code uses `__int128`.

```sh
cmake --preset release && cmake --build --preset release -j && ctest --preset release
cmake --preset sanitizers && cmake --build --preset sanitizers -j && ctest --preset sanitizers
```

## Commands

Configuration comes from `HLA_*` environment variables; see [`config.example.env`](config.example.env). On Windows, use `scripts/hlarb.ps1`.

```sh
hlarb check                                   # every live preflight check, read-only
hlarb dry-run [--duration-seconds N] [--sim-mt5]   # full pipeline, nothing sent; --sim-mt5 mirrors HL as MT5
hlarb live [--duration-seconds N]
hlarb report <journal>...                     # fills, fees, hedge latency, markouts, halts
hlarb drill                                   # failure drills
hlarb mt5-roundtrip --symbol S [--lots L]     # broker order latency (real orders)
hlarb markets | probe                         # HL instruments; HL feed cadence and RTT
```

## Verified so far (macOS, Kigali)

- 137 unit checks pass, including SDK signing vectors for every action. All 16 drills pass under release and ASan/UBSan.
- `dry-run --sim-mt5` against live HL data, 8 pairs:
  - two-sided ALO quotes on every pair, and a clean shutdown;
  - with quotes forced to the touch, 35 simulated fills, 35 hedges, 0 halts.

Not yet run: the Windows build, the MQL5 services in a real terminal, and a signed order on mainnet. Those are RUNBOOK steps 2–5 on the box.
