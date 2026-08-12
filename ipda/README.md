# IPDA signal service

Stores the full TradingView **IPDA_Full** Pine indicator and ports only the **IPDA
buy/sell** Supertrend entry into a Python signal service that submits market orders to
`[mt5-trader](../mt5-trader)`.

## What it does

1. **Polls** a market-data endpoint for **1-minute** OHLC candles.
2. **Aggregates** those into a configurable **target timeframe** (`TARGET_TF_MINUTES`, 5M).
3. **Recomputes** the IPDA Supertrend entry on every poll (forming-bar aware).
4. **Gates** on the configured trading sessions — executes inside them, notifies outside.
5. **Submits** each executable entry to mt5-trader (`POST /v1/signals`, `X-API-Key`).
6. **Watches** filled trades and notifies once when they reach the break-even trigger.



### The ported signal — Buy Chance / Sell Chance (`file.txt` Section 11)

The live trigger is the Pine's **Reversal** signal, whose labels read "Buy Chance" and
"Sell Chance":

```
rev_src = rsi(close, REVERSAL_SENSITIVITY)      # 14
BUY  ("Buy Chance")  = crossover (rev_src, REVERSAL_OVERSOLD)    # 25
SELL ("Sell Chance") = crossunder(rev_src, REVERSAL_OVERBOUGHT)  # 75
```

`rev_src` is Wilder's RSI — the Pine builds it from `ta.rma` of the up/down changes
rather than calling `ta.rsi`, and keeps two guards a bare RSI does not have (an all-gains
window reports 100, all-losses reports 0). Both are reproduced faithfully.

**Crossing is the trigger, not the level.** RSI merely sitting below 25 produces nothing;
it has to cross back up through it.

> **On the TradingView chart**, these labels are gated behind `enableReversal`, which
> ships **`false`**. Tick **Reversal Signal** under *IPDA Settings* to see the same labels
> the service trades, and set **Reversals Sensitivity** to 14 with levels 25 / 75.

**Stop-loss** and **take-profit** are fixed pip distances — `STOP_LOSS_PIPS=40`,
`TAKE_PROFIT_PIPS=50` — sent to mt5-trader as *distances*, so the broker anchors them to
the actual fill price rather than to the bar close. `USE_HARD_TARGETS` must stay `true`:
RSI yields no price level, so there is no indicator-derived stop to fall back on, and the
service refuses to start otherwise. `RISK_REWARD` is inert.

No confluence overlays (Range Filter, SuperIchi, TBO, etc.) and no vetoes.

### The other trigger: IPDA Supertrend (`file.txt` Section 5)

The ▲/▼ Supertrend×SMA entry is also ported and tested, but **is not what fires an
order**:

```
supertrend(close, sensitivity=5.5, atrLen=11)
sma = ta.sma(close, 13)
BUY  = crossover(close, supertrend)  and close >= sma
SELL = crossunder(close, supertrend) and close <= sma
```

`atrLen=11` and the SMA length of 13 are hardcoded in the Pine call — the Pine source
names that SMA `sma9` while its length is 13. `SUPERTREND_*` and `SMA_LEN` sit at the Pine
defaults and are unused unless `_InstrumentPipeline` in [service.py](src/service.py) is
switched back to `SupertrendSignalStrategy`.

### Opposite-direction signals

There is no flip logic. If a position is open and the opposite signal fires, the service
submits a new market order and lets the broker decide: a hedging account holds both, a
netting account offsets. Nothing is closed first.

### Forming-bar firing and repaint

The service evaluates the **forming** target-timeframe bar and fires the first signal it
sees mid-candle, then locks that bucket so at most one entry is produced per candle.

TradingView's alert on the same indicator uses `alert.freq_once_per_bar_close`, so it only
confirms at the close. **A label that appears two minutes into a 5M bar and disappears
before the bar closes still produces a live trade here.** That is the deliberate trade-off:
a better entry price in exchange for occasionally trading a signal that repaints away. Set
the target timeframe lower, or switch to close-confirmed evaluation, if that is not
acceptable.

## Trading sessions

Signals outside the configured sessions are logged and notified but **never executed**.


| Session    | Window      | Zone               |
| ---------- | ----------- | ------------------ |
| `tokyo`    | 09:00–18:00 | `Asia/Tokyo`       |
| `new_york` | 08:00–17:00 | `America/New_York` |


Windows are defined in the exchange's own timezone and compared after converting the
current instant into that zone, so daylight saving is handled by the tz database instead of
drifting an hour twice a year. Weekends are excluded. Boundaries are half-open: a window
includes its start minute and excludes its end minute.

```bash
TRADING_SESSIONS=tokyo,new_york            # empty = trade around the clock
SESSION_TOKYO=Asia/Tokyo:09:00-18:00
SESSION_NEW_YORK=America/New_York:08:00-17:00
```

The Deriv profile ships with `TRADING_SESSIONS=` (empty), because synthetic indices trade
continuously and cash-session hours mean nothing for them.

`zoneinfo` needs a tz database and **Windows ships none** — the `tzdata` package is a hard
dependency for that reason.

## Notifications

Sends to the shared `[notification-service](../notification-service)`
(`POST /notifications`), the same service mt5-trader uses. Two events are notified:

- `signal_skipped_out_of_session` — a signal fired outside the trading sessions and was
not executed. One notification per candle, not per poll.
- `break_even_reached` — a filled trade reached the MFE trigger.

```bash
NOTIFICATIONS_ENABLED=true
NOTIFICATION_SERVICE_URL=http://127.0.0.1:3010
NOTIFICATION_API_KEY=...
NOTIFICATION_CHANNELS=TELEGRAM              # TELEGRAM, EMAIL, SMS, WHATSAPP
```

A notification failure is logged as `notification_failed` and never interrupts trading.

## Break-even advisory (MFE)

When a filled trade reaches `MFE_BREAK_EVEN_PIPS` (default **30**) of favourable
excursion, the service sends one notification telling you to move the stop to entry.

**It is advisory. Nothing moves the stop.** mt5-trader exposes no position-modification
endpoint, so the operator does it in the terminal. Two consequences follow:

- Excursion is sampled once per `POLL_INTERVAL_SECONDS`. A spike that touches the trigger
and retraces inside one interval is missed.
- The tracker cannot see a real close, so it *infers* one when price has travelled the
take-profit distance in favour or the stop-loss distance against, or after
`TRACKED_TRADE_TTL_HOURS` (default 24). An inferred close is a guess about the broker's
state, not a fact — the terminal is authoritative.

Tracked trades are persisted to `{LOGS_DIR}/open_trades.json` and reloaded at startup, so a
restart keeps watching, and a trade that was already alerted is not alerted again.

```bash
TRACK_OPEN_TRADES=true
MFE_BREAK_EVEN_PIPS=30
TRACKED_TRADE_TTL_HOURS=24
```



## Pip sizing

Every pip-denominated setting (`STOP_LOSS_PIPS`, `TAKE_PROFIT_PIPS`, `MFE_BREAK_EVEN_PIPS`)
is multiplied by `PIP_SIZE`, so **set** `PIP_SIZE` **explicitly per instrument**:


| Instrument          | `PIP_SIZE` | `PRICE_DIGITS` |
| ------------------- | ---------- | -------------- |
| EURUSD              | `0.0001`   | 5              |
| XAUUSD              | `0.10`     | 2              |
| Volatility 75 Index | `0.01`     | 2              |


The Deriv profile deliberately keeps a wider 50/80 stop and target. At `PIP_SIZE=0.01`, a
40-pip stop is a 0.40 price distance on Volatility 75 — well inside that symbol's
`trade_stops_level` — and mt5-trader rejects it with `stop_loss_too_close`.

## Pine Script (TradingView)

`file.txt` is the full combined indicator (`IPDA + MS + OB + FVG + TrendLines`). Paste it
into TradingView → Pine Editor → Add to chart.

To make the chart show what the service trades:

1. Set the chart timeframe to **5M**.
2. *IPDA Settings* → tick **Reversal Signal** (it ships off, so the Buy Chance / Sell
   Chance labels are hidden by default).
3. *Reversal Settings* → **Reversals Sensitivity** 14, **Reversal Down Level** 75,
   **Reversal Up Level** 25.
4. *Alert Settings* → **🔔 Buy Chance (Reversal Up)** and **🔔 Sell Chance (Reversal
   Down)**.

Note that TradingView's alerts confirm at bar close while the service fires mid-candle,
so the chart alert will lag an executed entry by up to one 5M bar.

## Python service



### Install

```bash
cd ipda
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```



### Configure

```bash
cp .env.example.forex .env          # or .env.example.deriv -> .env.deriv
# edit DATA_API_KEY / MT5_SIGNAL_API_KEY / QUOTE / MT5_SYMBOL / PIP_SIZE
```



### Run

```bash
ipda                    # loads .env
ipda --profile deriv    # loads .env.deriv
```

Run exactly one instance per profile. Two instances against one profile duplicate every
trading decision.

### Tests

```bash
pytest
```



## Layout


| Path                                        | Role                                                    |
| ------------------------------------------- | ------------------------------------------------------- |
| `file.txt`                                  | Full IPDA Pine v5 indicator                             |
| `src/`                                      | Signal service modules (installed / imported as `ipda`) |
| `src/sessions.py`                           | Exchange-local, DST-aware session windows               |
| `src/notifier.py`                           | notification-service client                             |
| `src/position_tracker.py`                   | MFE watcher for the break-even advisory                 |
| `.env.example.forex` / `.env.example.deriv` | Profile templates                                       |
| `symbols.example.*.json`                    | Multi-instrument manifests                              |
| `docs/operator-runbook.md`                  | Deployment and day-to-day operation                     |
| `tests/`                                    | Unit tests                                              |




## Notes vs lux-algo

`[lux-algo](../lux-algo)` ports the same Supertrend×SMA trigger **plus** optional overlay
confluence. This project keeps the raw IPDA labels only and uses signal source `ipda`
(accepted by mt5-trader's `SignalSource` enum).