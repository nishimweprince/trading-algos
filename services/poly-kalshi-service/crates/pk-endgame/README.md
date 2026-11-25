# pk-endgame

In the last minute of a Polymarket 15-minute Up/Down window, most of the
number it settles on is already printed. Polymarket resolves Up when the mean
of the Chainlink prices in the 60 s before the close is at least the mean of
those in the 60 s before the open (RESULTS.md: 99.7% of 24,321 windows). With
40 of those 60 seconds printed, only 20 are uncertain. This process prices
what is left and buys the side the book still underprices.

PLAN 4.3 tested this fair value and found it lost, but its scan stopped 60 s
before the close (`EARLY..=900 − LATE`, `LATE = 60`), so the endgame itself
was never tested. This is that test, forward and on live data.

## The rule (epoch 2, PREREG.md)

| | Default (`PK_EG_*`) |
|---|---|
| Window open average K: Chainlink seconds `[open − 60, open − 1]`, ≥ 45 of 60 seen | `MIN_OPEN_TICKS` |
| Fair P(Up): seen seconds of the closing average plus a driftless walk for the rest (model.rs); sd ×1.5 while more than 30 s remain | `SIGMA_WINDOW_S` 300, `SD_FLOOR`, `SD_MULT_EARLY`, `SD_MULT_FROM_S` |
| Entries from 60 s to 2 s before the close, newest tick ≤ 3 s old | `ENTRY_FROM_S`, `ENTRY_UNTIL_S`, `MAX_TICK_AGE_MS` |
| Buy the side with P − ask − taker fee between 4¢ and 12¢, ask within 0.05–0.70 | `MIN_EDGE`, `MAX_EDGE`, `MIN_PX`, `MAX_PX` |
| Kalshi agrees: its mid for the same side − ask − fee ≥ 0, book ≤ 3 s old, spread ≤ 0.05 | `KALSHI_FILTER`, `KALSHI_MIN_MARGIN`, `KALSHI_MAX_AGE_MS`, `KALSHI_MAX_SPREAD` |
| FAK at ask + 0.01, never past the price leaving 0.02 of edge | `LIMIT_OFFSET`, `MIN_NET_EDGE` |
| 10 shares, one entry per window and coin | `SHARES`, `MAX_ENTRIES_PER_WINDOW` |
| Open cost ≤ $50; realised −$15 today halts until the next UTC day or `ACK` | `MAX_OPEN_COST_USD`, `DAILY_LOSS_USD` |

Epoch 1 (the first 16 h) used edges ≥ 4¢ with no cap, asks to 0.97, no
Kalshi, ≥ 55 opening ticks: 57 settled, +2.42¢/share, t 0.42. Why each rule
changed is in PREREG.md. The page and `eg_review.py --epoch 2` read one
epoch at a time.

The fee is each market's own (Gamma `feeSchedule`): BTC 0.05, the others 0.01
as of 2026-11-24.

## Feeds

- **Chainlink**: Polymarket's public RTDS socket (`crypto_prices_chainlink`),
  one tick per coin per second, stamped on whole seconds. No credentials.
- **Books**: the CLOB market channel, through `pk-feeds`.
- **Kalshi**: the authenticated `orderbook_delta` channel (read-only key,
  `pk-feeds`' Kalshi socket with sequence-gap resync) on each window's
  `KX<ASSET>15M` market. Kalshi settles on CF Benchmarks' BRTI with the same
  60 s averages, so it is a fast, informed second opinion, not the fair value.
- **Spot (recorded only)**: Coinbase `ticker` and Binance `bookTicker` best
  bid/ask. Each `eg_sec` line and decision carries the mids and `spot.p_up`:
  the same model with the seconds still to come starting from the newest
  Chainlink tick moved by spot's change since that tick's second (Chainlink
  trails spot by ~2 s). No rule reads it; it exists to test whether a
  spot-aware model forecasts better than Polymarket's own price.
- **Windows and outcomes**: Gamma. Positions settle on Polymarket's own
  outcome; ours is journaled beside it (`eg_resolution.agree`).

The process must be up at a window's open to trade that window (it needs the
opening average). Polymarket publishes `eventMetadata.priceToBeat` only after
the close.

## Paper and live

- **Paper (default).** An entry fills `PK_EG_PAPER_LATENCY_MS` (250) after the
  decision against the book as it is then, so makers that reprice in the
  meantime are counted. The instant fill is journaled beside it.
- **Live.** `PK_EG_MODE=live` and `PK_EG_LIVE_CONFIRM=yes`, plus a wallet.
  Orders are FAK buys through the trader's `LiveExec`; settled winners are
  redeemed through the relayer (deposit wallet) or on-chain (EOA). Give it a
  wallet of its own, not pkm's.

**Gate before live:** ≥ 150 settled positions over ≥ 3 days including US
hours; net ¢/share > 0 with t ≥ 2 (window-clustered); TWAP agreement ≥ 99%;
calibration within a few points in the bands that trade.

## Run

```sh
cargo build --release -p pk-endgame
cp crates/pk-endgame/endgame.example.env /etc/pk-endgame.env   # chmod 600
target/release/pk-endgame --env /etc/pk-endgame.env run
python3 crates/pk-endgame/eg_review.py data/state/endgame/journal
```

As a service on the Dublin host: `crates/pk-endgame/pk-endgame.service`
(user `poly`, cores 0-1, off pkm's hot cores).

| | |
|---|---|
| `touch data/state/endgame/KILL` | no new entries (remove to resume) |
| `touch data/state/endgame/ACK` | clear today's loss halt |
| `pk-endgame --env FILE check` | live settings: authenticate and read the cash, send nothing |

## Monitoring page

`http://127.0.0.1:8095/` (`PK_EG_UI_ADDR`), behind HTTP Basic auth with
`PK_EG_UI_TOKEN` as the password. It shows the P&L after fees (hero number
and cumulative chart), progress against each go-live condition, results by
coin, by seconds before the close and by model probability (calibration),
why windows did not trade, feed health, and the latest decisions. It
refreshes every 10 s. `GET /api/summary` is the same data as JSON; `/health`
needs no password.

The summary is rebuilt from the journal at start (skipping `eg_sec`), so a
restart loses nothing.

## The Contabo dry run (France: paper only)

Polymarket blocks opening positions from France (`/api/geoblock` answers
`"blocked": true` for the VM), and live mode checks that answer and refuses
to start there. Paper sends nothing, so it runs there until the dedicated VM
exists.

| | |
|---|---|
| Unit | `pk-endgame.contabo.service` → `/etc/systemd/system/pk-endgame.service`, user `pkeg`, nice 10, one core, 512 MB |
| Binary | `/opt/pk-endgame/bin/pk-endgame` |
| Settings | `/etc/pk-endgame.env` (paper, no wallet keys) |
| State and journal | `/opt/pk-endgame/data/state/endgame` |
| Page | `127.0.0.1:8095`, password in `/etc/pk-endgame.env` (`PK_EG_UI_TOKEN`) |

Moving to the dedicated VM: copy `data/state/endgame` across and the run
continues where it stopped.

## Journal (`data/state/endgame/journal/YYYY-MM-DD.jsonl`)

| Record | |
|---|---|
| `eg_window` | a window found on Gamma, with its tokens and fee |
| `eg_sec` | each second of the last 90: the model and both books |
| `eg_decision` | an entry, with everything it saw |
| `eg_fill`, `eg_no_fill` | what the order got (`held_ms`, `instant_*`) |
| `eg_close` | K, the closing TWAP, our outcome, the last gate, the best edge seen |
| `eg_resolution` | Polymarket's outcome against ours |
| `eg_settle` | a position's P&L |
| `eg_halt`, `eg_ack`, `eg_kill`, `eg_redeem`, `eg_status` | risk and housekeeping |

## The weekly re-fit

`eg_refit.py <state>/journal --days 7 --out <state>/refit.json` fits the sd
multiplier per seconds-left band from `eg_sec` and writes what it finds; the
page shows it. On the VM it runs Mondays (`pk-endgame-refit.timer`). It only
reports: adopting a new multiplier starts a new epoch.

`eg_sec` is a forward tape of the closing stretch: thresholds can be re-scored
from it later without a new run.
