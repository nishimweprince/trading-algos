# SupertrendConfluenceEA

A self-contained MetaTrader 5 Expert Advisor that reproduces the entry logic of the Pine v5
chart study in [`../file.txt`](../file.txt) natively inside the terminal — no HTTP bridge, no
Python service, no external dependencies.

It is a native port of the Python reference implementation in [`../src/lux_algo/`](../src/lux_algo/),
function by function, so both engines produce the same series.

## The signal

```
supertrend  = Supertrend(close, factor = 5.5, ATR length = 11)   // Wilder-smoothed ATR
confirmSma  = SMA(close, 13)

BUY   when close crosses ABOVE supertrend and close >= confirmSma
SELL  when close crosses BELOW supertrend and close <= confirmSma
```

Every signal is then put through a **confluence gate** and, optionally, two **counter-trend
vetoes**.

### Overlays (the confluence gate)

| Overlay | Source lines in `file.txt` | Bullish when | Enabled by default |
|---|---|---|---|
| Range Filter | 202–325 | filter direction is upward | yes |
| SuperIchi | 328–380 | Tenkan > Kijun | yes |
| TBO | 382–429 | EMA(20) > EMA(40) | yes |
| Smart Trail | 431–501 | trend state is +1 | yes |
| Heikin-Ashi bias | 148–200 | smoothed HA close > HA open | no |
| MACD regime | 680–738 | MACD > 0 and histogram > 0 | no |
| Parabolic SAR | 66, 78 | SAR below the open/close midpoint | no |

`InpConfluenceMode`:

- **Threshold** *(shipped default, `InpConfluenceThreshold = 3`)* — at least N enabled overlays
  must point the same way as the signal. `0` means "all of them".
- **Unanimous** — every enabled overlay must agree. This is the Python service's default and is
  considerably more selective.
- **Off** — no gating at all; reproduces the raw ▲/▼ decision of the original study exactly.

The default is Threshold rather than Unanimous because unanimity across four overlays produces
very few trades, and an MQL5 Market product has to demonstrably trade during validation. Switch
to Unanimous if you want parity with the Python service's defaults.

### Vetoes (both off by default)

- `InpVetoTpPoints` — an opposing exhaustion take-profit point (the `lele` function, lines 98–146)
  blocks the entry.
- `InpVetoReversals` — an opposing RSI reversal (lines 503–517) blocks the entry.

## Signal timing

Signals are evaluated **on bar close only**. The EA reads the bar that has just completed and
never the forming bar, so nothing repaints. This differs deliberately from the Python service,
which evaluates the forming bar mid-candle and locks it after the first signal.

The first bar close after attaching is used only to establish a baseline — the EA does not act on
a bar that closed before it was loaded.

## Installation

1. Copy `SupertrendConfluenceEA.mq5` into `MQL5/Experts/` in your terminal's data folder
   (*File ▸ Open Data Folder* in MetaTrader 5).
2. Open it in MetaEditor and compile (F7).
3. Attach it to a chart, or select it in the Strategy Tester.

Nothing else is required: the EA has no `#include` beyond the standard trade classes, reads no
files, and makes no network calls.

## Inputs

### Signal

| Input | Default | Meaning |
|---|---|---|
| `InpTimeframe` | `PERIOD_CURRENT` | Strategy timeframe, independent of the chart |
| `InpSensitivity` | `5.5` | Supertrend ATR factor |
| `InpAtrLen` | `11` | Supertrend ATR length |
| `InpSmaLen` | `13` | Confirmation SMA length |
| `InpHistoryBars` | `1500` | Bars recalculated on each bar close |
| `InpMinWarmupBars` | `300` | Bars required before any trading |

`InpHistoryBars` matters: Supertrend, Range Filter, Smart Trail and Parabolic SAR are all
recursive, so their values converge over history rather than being exact from bar one. A larger
window tracks a TradingView chart more closely at a small CPU cost.

### Stops, targets and sizing

| Input | Default | Meaning |
|---|---|---|
| `InpUseStopLoss` / `InpUseTakeProfit` | `true` | Attach each exit |
| `InpRiskReward` | `2.0` | Take profit as a multiple of the stop distance |
| `InpUseHardTargets` | `false` | Use fixed money targets instead of Supertrend/RR |
| `InpHardSlMoney` / `InpHardTpMoney` | `25` / `40` | Money targets, account currency |
| `InpMinStopPoints` | `0` | Floor for the stop distance (0 = broker minimum) |
| `InpSizingMode` | Percent of equity | Fixed lot or percent-risk sizing |
| `InpFixedLot` | `0.10` | Lot used in fixed-lot mode |
| `InpRiskPercent` | `1.0` | Equity risked per trade in percent mode |
| `InpMaxLot` | `0` | Hard lot cap (0 = broker maximum) |

The default stop is the **Supertrend line itself** — the study's own trailing stop — and the
target is `InpRiskReward` times that distance. Fixed money targets need a known lot size, so
`InpUseHardTargets` requires fixed-lot sizing; the EA refuses to initialise on the invalid
combination rather than guessing.

Lot sizes are derived from the symbol's tick value and tick size, normalised to the broker's
volume min/max/step, and reduced until the required margin fits the free margin.

### Trade management and guards

| Input | Default | Meaning |
|---|---|---|
| `InpTrailMode` | Off | `ATR distance` or `Smart Trail overlay line` |
| `InpTrailAtrMult` / `InpTrailAtrPeriod` | `2.0` / `14` | ATR trailing distance |
| `InpBreakEvenAtR` | `0` | Move the stop to break-even at N × risk (0 = off) |
| `InpBreakEvenOffsetPt` | `5` | Break-even offset in points |
| `InpCloseOnOpposite` | `false` | Close open trades when the opposite signal fires |
| `InpMaxPositions` | `1` | Concurrent position cap |
| `InpOnePerDirection` | `true` | At most one position per direction |
| `InpMinBarsBetween` | `0` | Minimum bars between entries |
| `InpMaxSpreadPoints` | `30` | Spread ceiling (0 = no limit) |
| `InpMaxDailyLossPct` | `0` | Daily loss limit, % of the day-start balance (0 = off) |
| `InpUseSessionFilter` + hours/days | off, 07–20 | Trading window, server time; midnight wrap supported |

Stops are never moved backwards, never trailed inside the broker's stop or freeze level, and the
initial risk of each position is tracked per ticket so `InpBreakEvenAtR` measures real R multiples.
After a terminal restart the risk is reconstructed from the position's current stop.

### Execution and diagnostics

`InpMagicNumber`, `InpSlippagePoints`, `InpOrderComment`, `InpOrderRetries` behave as usual.
Requotes, price changes and off-quotes are retried up to `InpOrderRetries` times; invalid stops
and every other retcode are logged and the entry is abandoned.

`InpVerboseLog` logs the reason each candidate signal was rejected. `InpDebugBars` prints the
close, Supertrend and SMA of the last N bars plus every overlay direction — this is the tool for
checking parity against the Python port.

## Backtesting

Use **Every tick based on real ticks** where your broker provides it. Signals are computed once
per bar close, so the tick model only affects fill prices and trailing behaviour, not the signal
itself.

Suggested first run: EURUSD, M5, three months, default inputs. Expect the confluence gate to
reject most raw Supertrend crossings — that is what it is for. If a run produces no trades at
all, drop `InpConfluenceThreshold` to `2`, or set `InpConfluenceMode` to `Off` to confirm the raw
trigger is firing before re-tightening.

## Checking parity with the Python port

1. Set `InpDebugBars = 20` and run a single pass.
2. Feed the same candles through `src/lux_algo` — `indicators.supertrend`, `indicators.sma`, and
   each `overlays.*_dir` function.
3. Compare the printed values.

Late-window values should match closely. Early bars in the window drift because the recursive
series are still converging; increase `InpHistoryBars` if the tail does not agree.

Two seeding details are inherited from the Python reference on purpose so the two engines agree
with each other: the EMA seeds on its first sample (Pine's `ta.ema` seeds with an SMA), and the
Wilder MA used by Smart Trail seeds from zero. Both converge well inside the warmup window. If
you change either, change it in both places.

## Publishing on the MQL5 Market

The code is written to the Market validator's rules:

- No file access, no `WebRequest`, no DLL imports, no external resources.
- `OnInit` validates every input and returns `INIT_PARAMETERS_INCORRECT` on bad values.
- No hardcoded symbol, digits, point value or contract size — all read from `SymbolInfo*`.
- Stops normalised to the symbol's digits and pushed outside the stop and freeze levels.
- Filling mode selected from the symbol via `CTrade::SetTypeFillingBySymbol`.
- Trade retcodes handled explicitly, with a bounded retry budget and no infinite loops.
- No `Alert`, `MessageBox` or `Sleep`; diagnostics go to the Experts log through `Print`.

Before submitting:

1. Compile with **zero errors and zero warnings** — the validator rejects warnings.
2. Run *Check for Market* in MetaEditor (right-click the EA in the Navigator) and clear every
   finding.
3. Test on several symbols and timeframes, including an exotic and a metal, to confirm the
   symbol-agnostic sizing holds.
4. Prepare the listing: description, at least one backtest screenshot, and the recommended
   symbol/timeframe/deposit.

### Naming and intellectual property

`file.txt` is a copy of a commercial, trademarked third-party chart study. This EA is
deliberately published under a neutral, descriptive name with **no third-party branding** in the
product name, `#property` strings, input labels or description, and the code comments reference
the algorithm and its source line numbers rather than the vendor. Keep it that way: listing a
port under the original vendor's trademarked name would very likely be rejected and would create
real legal exposure. Satisfying yourself that you have the right to sell a port of this logic is
your call, not something this file settles.
