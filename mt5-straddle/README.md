# MT5 two-account news straddle

`straddle.py` places a **buy stop on account 1** and a **sell stop on account 2**
`ARM_SECONDS_BEFORE` seconds before the release time in `.env` (GMT). When one leg fills, the
other account's pending order is cancelled (OCO). The filled leg gets a breakeven move and a
trailing stop, with an optional max hold time. Unfilled orders are cancelled after `PENDING_EXPIRY_SECONDS`.

Either account can instead run `SIDE=BOTH`, a full straddle inside that one account.

## Windows setup (once)

1. Install Python 3.10+ (64-bit) from python.org and tick "Add to PATH".
2. Install **two separate MT5 terminals**, one per account. The Python API drives one
   terminal per process, and both accounts need to be logged in at the same time.
   - Run the broker's MT5 installer twice and pick a different folder each time,
     for example `C:\MT5_Account1` and `C:\MT5_Account2`, or
   - copy an installed MT5 folder and set `ACCn_PORTABLE=true`.
3. Open each terminal, log in to its account, and turn on **Algo Trading** (toolbar button).
   In Tools → Options → Expert Advisors, allow algorithmic trading.
4. In a terminal (PowerShell or cmd), from this folder:

```bash
py -m pip install -r requirements.txt
```

```bash
copy .env.example .env
```

5. Edit `.env`: logins, passwords, servers, the two `terminal64.exe` paths, the symbol
   (add the broker suffix with `ACCn_SYMBOL` if there is one), and `EXECUTION_TIME_GMT`.
6. Sync the Windows clock. The release time comes from the PC clock, not the broker:

```bash
w32tm /resync
```

## Running

Check connections, symbol, sizing and `order_check` (no orders sent):

```bash
py straddle.py --check
```

Test on demo accounts with `DRY_RUN=false`, releasing 60 seconds from now:

```bash
py straddle.py --in 60
```

Real run at `EXECUTION_TIME_GMT`. Start it a few minutes early and leave the window open:

```bash
py straddle.py
```

Run only one account:

```bash
py straddle.py --account 1
```

`DRY_RUN=true` (the default) runs the full flow but only calls `order_check`. Set
`DRY_RUN=false` to send orders.

Ctrl+C cancels all pending orders. Open positions keep their SL/TP, and are closed too if
`CLOSE_ON_EXIT=true`. Logs go to the console and to `logs/straddle_<date>_acc<n>.log`, in GMT.

## What happens at the release

| Time | Action |
|---|---|
| release − `ARM_SECONDS_BEFORE` | Checks the connection, waits for spread ≤ `MAX_SPREAD_POINTS`, places the stop orders. If the spread is still too wide 1 s before release, nothing is placed. |
| fill | Logs the fill price and slippage against the order price. With `OCO=true`, cancels the opposite pending order on the other account. |
| in profit ≥ `BREAKEVEN_TRIGGER_POINTS` | SL moves to entry + `BREAKEVEN_OFFSET_POINTS`. |
| in profit ≥ `TRAIL_START_POINTS` | SL trails `TRAIL_DISTANCE_POINTS` behind price, moving in steps of at least `TRAIL_STEP_POINTS`. It never moves backwards and stays outside the broker's stop/freeze level. |
| release + `PENDING_EXPIRY_SECONDS` | Unfilled pending orders are cancelled. |
| fill + `MAX_HOLD_SECONDS` | The position is closed at market. |
| nothing left open | Logs net P&L including commission, then exits. |

## Sizing

With `LOT_SIZE=0`, lots are set so that hitting the SL, plus `SIZING_SLIPPAGE_POINTS` of
slippage and `COMMISSION_PER_LOT`, loses `RISK_PERCENT` of balance. The broker's own
`order_calc_profit` is used, so it works for FX, gold and indices. Either way, one order is
capped at `MAX_MARGIN_PCT` of free margin, then rounded down to the broker's lot step.

## Things to know

- Distances are in **points**. On 5-digit EURUSD, 10 points = 1 pip. On 2-digit XAUUSD,
  100 points = $1, so the EURUSD defaults are far too tight for gold.
- Raw/razor accounts have near-zero spread in normal conditions, but it still widens at the
  release, and stop orders fill as market orders, so slippage past the trigger is normal.
- If both legs trigger in a whipsaw before OCO cancels, both positions run with their own SL.
- Using two accounts to hedge each other can break some brokers' terms. Check yours.
- Trailing only runs while the script runs. If it stops, the last SL set stays on the server.
