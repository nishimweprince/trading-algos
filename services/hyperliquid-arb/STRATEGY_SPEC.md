# Strategy specification

This file states exactly what `hlarb` computes. The code is in [`src/engine.cpp`](src/engine.cpp), and every rule here is exercised by `hlarb drill`.

## Setting

- Hyperliquid (HL) is the slow leg:
  - `bbo` pushes about every 100 ms;
  - an order waits about 200 ms for block inclusion;
  - xyz FX reference prices are pushed about every 3 s.
- We rest maker (ALO) quotes on HL, priced from MT5, and hedge every HL fill on MT5 at once.
- HL processes cancels before new orders inside a block. That protects our resting quotes from takers, but it would work against us as takers.
- We never trade on a stale MT5 price, so this is not broker latency arbitrage.

Units:
- HL size is in coin units. HL perps are linear in USDC: P&L = `size × Δprice`.
- MT5 size is in lots. The publisher reports `tick_value` / `tick_size`, and `vpp` (the account-currency value per lot per 1.0 price move) is their ratio. `vpp` varies with price for cross-currency symbols such as USDJPY, so it is refreshed every second.
- `ratio` comes from `HLA_PAIRS` (default 1), with `hl_price ≈ ratio × mt5_price`.

## Hedge ratio and quote size

```
hl_units_per_lot_step = mt5_volume_step × vpp / ratio
quote_size            = round_down(HLA_QUOTE_LOTS × hl_units_per_lot_step, hl_step)
target_mt5_lots       = −hl_units × ratio / vpp
unhedged_lots         = mt5_lots + hl_units × ratio / vpp
```

A pair whose `quote_size` is below one HL step, or below $10 notional, is blocked (`size`).

Example: USDJPY at 157.8 has `vpp` ≈ 633.7 USD per lot per yen. One 0.01-lot step is therefore 6.33 xyz:JPY units, about $1k.

## Fair value

```
raw_basis = hl_mid − ratio × mt5_mid               (on every HL bbo or MT5 tick)
basis    += (1 − exp(−dt / HLA_BASIS_TAU_S)) × (raw_basis − basis)
fair      = ratio × mt5_mid + basis
```

The basis absorbs a static or slowly drifting offset:
- CFDs on futures against an xyz oracle that blends cash and futures;
- USDC against USD;
- funding drift.

## Quotes

```
half_spread_ppm = hl_maker_ppm + HLA_MT5_COMMISSION_PPM + HLA_ADVERSE_PPM + HLA_EDGE_PPM
                + ½ × mt5_spread / mt5_mid × 1e6
inventory       = hl_units / hl_units_per_lot_step               (in lot steps, signed)
skewed          = fair × (1 − inventory × HLA_SKEW_PPM / 1e6)
tick            = hl_tick(fair, szDecimals)                      (5 significant figures, ≤ 6 − szDecimals decimals)
bid             = round_down(skewed × (1 − half_spread_ppm/1e6), tick),  capped at hl_best_ask − tick
ask             = round_up  (skewed × (1 + half_spread_ppm/1e6), tick),  floored at hl_best_bid + tick
```

`hl_maker_ppm` is the account's `userAddRate`, doubled on HIP-3 dexes and divided by 10 in growth mode, unless `HLA_HL_MAKER_PPM` overrides it.

Side limits:
- The bid is quoted only if `inventory + HLA_QUOTE_LOTS ≤ HLA_MAX_INVENTORY_LOTS`.
- The ask is quoted only if `inventory − HLA_QUOTE_LOTS ≥ −HLA_MAX_INVENTORY_LOTS`.
- If total HL gross notional plus this quote would exceed `HLA_MAX_GROSS_NOTIONAL_USD`, only the side that reduces the position is quoted.

Requoting:
- A resting quote moves, through `batchModify` for both sides in one action, when its target moves by at least `max(tick, price × HLA_REQUOTE_PPM)` or its size changes.
- Moves are at most one per `HLA_MIN_REQUOTE_MS`.
- At most one command per side is in flight.
- Places, modifies and cancels carry a client order id (cloid).
- An ack timeout, or a failed modify, cancels by cloid, because the order's state is unknown.

## Per-pair blocks

A blocked pair has no quotes. The first condition that holds sets the block:

| Block | Condition |
|---|---|
| `no_spec` | no MT5 symbol spec yet |
| `mt5_stale` | no MT5 tick for `HLA_MT5_TICK_STALE_MS` |
| `mt5_closed` | the symbol is not in a trade session, or `trade_mode` ≠ full |
| `mt5_spread` | MT5 spread > `HLA_MAX_MT5_SPREAD_PPM` |
| `hl_stale` | no `activeAssetCtx` for `HLA_HL_STALE_MS` (HL pushes ctx about once a second) |
| `no_hl_book` | HL book is one-sided |
| `basis_cold` | basis younger than `HLA_BASIS_WARMUP_S` |
| `rollover` | UTC time inside `HLA_ROLLOVER_UTC` |
| `mark_oracle` | \|mark/oracle − 1\| > `HLA_MAX_MARK_ORACLE_PPM` (discovery bounds, weekends) |
| `basis_large` | \|basis/fair\| > `HLA_MAX_BASIS_PPM`: wrong symbol or ratio |
| `fair_gap` | \|hl_mid/fair − 1\| > `HLA_MAX_FAIR_GAP_PPM`: dislocation or bad data |
| `size` | quote smaller than one HL step or $10 |

## Hedging

On every HL fill (and after every MT5 result):
- `steps = llround((target_mt5_lots − mt5_lots) / mt5_step)`.
- If `steps ≠ 0` and `|steps × step| ≥ volume_min`, one market order goes to MT5. It is IOC, FOK or return, depending on the symbol's filling mode, with `HLA_HEDGE_DEVIATION_POINTS`.
- Only one hedge per pair is in flight at a time.
- Orders older than `HLA_MT5_ORDER_MAX_AGE_MS` are dropped by the DLL and answered `expired`.

| Outcome | Action |
|---|---|
| filled / partial | book `mt5_lots`, hedge any remainder |
| rejected / error / expired | retry once; a second failure is an **emergency** |
| no result after `HLA_HEDGE_TIMEOUT_MS` | quotes pulled (`hedge_unknown`). If the executor's position scan shows the expected change, the fill is booked (`hedge_inferred`). |
| no result after `max(3 × timeout, max_age + 1 s)` | **emergency** |
| exposure ≥ ½ step that cannot be hedged (closed session, no spec) for that long | **emergency** |

**Emergency:** a hard halt, plus one reduce-only IOC on HL for the HL units that MT5 does not cover. It is priced at the touch, plus or minus `HLA_FLATTEN_SLIPPAGE_PPM`.

## Halts

Soft halts pull every quote and clear when their condition clears:

| Soft halt | Condition |
|---|---|
| `gateway_down` | HL private WebSocket down. Cancels go over REST meanwhile. |
| `bridge_stale` | publisher or executor heartbeat older than `HLA_MT5_HEARTBEAT_STALE_MS` |
| `mt5_account` | terminal disconnected, algo trading off, or login not in `HLA_MT5_LOGINS` |
| `aux_stale` | no HL account snapshot for `HLA_AUX_STALE_MS` |
| `budget` | HL address request budget minus requests since the snapshot < `HLA_MIN_REQUEST_BUDGET` |
| `margin` | any basket dex withdrawable < `HLA_HL_MIN_MARGIN_USD`, or MT5 free margin < `HLA_MT5_MIN_MARGIN_USD` |
| `hedge_unknown` | a hedge result is overdue |

Hard halts write the kill file, so a restart refuses to trade until a human removes it:

| Hard halt | Condition |
|---|---|
| `emergency` | see Hedging |
| `daily_loss` | HL account value + MT5 equity fell more than `HLA_MAX_DAILY_LOSS_USD` since 00:00 UTC (deposits and withdrawals count, so pause first) |
| `forced_fill` | an ADL or liquidation fill. It is still hedged. |
| `position_mismatch` | either check below disagrees, or a malformed private frame arrives (a fill may have been lost) |

Position mismatch checks:
- the MT5 position scan disagrees with the engine by ≥ ½ step for 5 s with no hedge settling;
- the HL clearinghouse disagrees by ≥ 1 HL step for 10 s.

The kill file can also be created by hand (`echo stop > C:\hlarb\hlarb.kill`). It is checked every 500 ms.

`scheduleCancel` (dead-man switch): the gateway sets HL to cancel everything at now + `HLA_DEADMAN_S`, and re-arms it every `HLA_DEADMAN_S / 2` only while the decision thread's heartbeat is fresh. A hung engine therefore has its orders cancelled within `HLA_DEADMAN_S`.

## Startup and shutdown

**Startup refuses** (exit 2 or 3) on any of these:
- a kill file exists;
- the key is the master wallet, or is not an approved agent;
- the MT5 login is not allowed, or algo trading is off;
- a pair's prices differ by more than `HLA_MAX_BASIS_PPM`;
- positions are not hedged (unless `HLA_ADOPT_POSITIONS=1`).

It then cancels any open HL orders on the basket coins and sets leverage: isolated where the market is isolated-only, else cross, at `HLA_HL_LEVERAGE`.

**Shutdown** (Ctrl-C, `--duration-seconds`, or a fatal pipeline error):
1. Pull quotes and wait up to 3 s for acks.
2. Sweep-cancel open basket orders over REST.
3. Clear the `scheduleCancel`.

Hedged positions are left open and listed in the `stop` journal record.
