# pk-pmus

The poly-kalshi gap rule with **Polymarket US** as the venue, paper only.
Nothing is ever sent to either venue.

Polymarket US and Kalshi list the same 15-minute crypto Up/Down contracts on the
same CF Benchmarks indexes (BRTI, ETHUSD_RTI, SOLUSD_RTI, XRPUSD_RTI), so
Kalshi's mid is used as the fair value for Polymarket US.

## Rule

- Coins: BTC, ETH, SOL, XRP (`cpc-btc`, `cpceth-eth`, `cpcsol-sol`, `cpcxrp-xrp` 15m series).
- From 60 s after the open to 60 s before the close, buy the Polymarket US side that is
  5–15¢ cheaper than Kalshi's mid after the taker fee (Θ·p·(1−p), Θ = 0.0695).
- Kalshi book fresh (≤ 3 s) with a spread ≤ 3¢; Polymarket US quote ≤ 1.5 s old.
- One entry per coin per window, 10 shares, limit = ask + 1¢ capped at fair − fee − 1¢.
- Paper fill 150 ms after the decision against the Polymarket US book fetched then.
- Settles on Polymarket US's `/settlement`; Kalshi's result is journaled beside it.
- Caps: $50 open cost. $15 daily loss, booked to the day of entry: in paper the breach
  is journaled (`pu_halt` with `record_only`) and entries continue; `PK_PU_ENFORCE_HALT=1`
  halts until `ACK`.

## Epoch 3: fresh books (2026-11-26)

**Epochs 1 and 2 are not evidence.** The gateway serves `/v1/markets/{slug}/book`
through Cloudflare with `cache-control: public, max-age=30`. Every poll and every paper
fill of epochs 1–2 could read a book up to 30 s old (`cf-cache-status: HIT`, `age: 12`
seen with a 12 s-old `transactTime`). That old book was then compared with Kalshi's live
book. Epoch 1's +$116 and epoch 2's +$18.86 measure that comparison, not an edge.

The CDN also caches the pre-open 404. Each market is `MARKET_STATUS_CLOSED` until its
window opens, so BTC books stayed 404 for up to ~2 minutes after each open. That
accounts for all 2,851 logged poll errors.

From epoch 3:

- **Cache-busted books.** Every book request carries `?_=<ns>` (a `Cache-Control:
  no-cache` header alone still gets a HIT). Responses are checked for `cf-cache-status`.
  A cached copy counts in `poll.cache_hits` (which should stay 0), and its receipt time
  is backdated by `Age`, so the 1.5 s age gate drops it.
- **One poller per window.** Each live window has its own task with its own interval
  (`PK_PU_POLL_MS`) and a 1 s request deadline, so a slow market delays only itself.
  A 404 in the first 30 s, or a state other than `MARKET_STATE_OPEN`, counts as
  `not_open` and backs off (250 ms → 1 s); later errors are counted by kind
  (`poll` on the page) and journaled once per window as `pu_book_err`.
- **Evaluated on quote arrival.** A new Polymarket US quote or a Kalshi delta evaluates its
  window at once (`trigger` on `pu_decision`); the 100 ms clock remains as a fallback.
- **Venue time.** `pu_decision` carries `pm_venue_ts` / `pm_venue_age_ms` (the book's
  `transactTime`, its last change, so a quiet book ages without being stale).
- **Delay study.** For each decision the book is also fetched at
  `PK_PU_PAPER_DELAYS_MS` (25, 50, 100, 150 ms) and the same order walked against it
  (`pu_fill_probe`, settled as `pu_probe_settle`). Only the `PK_PU_PAPER_LATENCY_MS`
  fill (150 ms) is a position. The page's *Arrival delay* table compares fill rate,
  shares filled, slippage and net ¢/share by delay and coin. Change size or hosting only
  after this has data.
- **Fee check.** Market JSON carries `feeCoefficient` (0.11 on the 15m crypto markets),
  while the gate uses Θ = 0.0695. Public reports also describe volume tiers, with
  0.095 under $10k a month. `pu_window.fee_coef` and `pu_decision.edge_at_market_fee`
  are journaled so the epoch can be re-scored at the market's coefficient. Settle which
  rate applies before going live.

## Measured, never traded (epoch 2)

- **Minimum edge.** Epoch 1 (3¢) lost on 3–4¢ gaps, so the rule is 5¢. The first gap of
  at least 3¢ in each coin-window is journaled as `pu_shadow` and settled at ask + fee
  (`pu_shadow_settle`), so the page can compare the 3¢ and 5¢ rules.
- **Sizing.** Every fill journals the depth at the limit, the levels under it, and what each
  rule would have bought against the same book (`sizes`; settled P&L in `pu_settle.alt`):
  flat 10; tiers 10/20/30 at < 8¢ / 8–11¢ / ≥ 11¢; quarter and tenth Kelly on Kalshi's mid;
  shrunk quarter Kelly on q_adj = c + s·(q − c) (`PK_PU_KELLY_SHRINK`, 0.5, never above 1).
  Sized rules are capped by depth, 2% of a $1,000 bankroll per trade and 5% per window
  across coins (the four coins in one window are close to one bet). The page shows each
  rule's P&L, cost risked, return on risk, worst drawdown and window-clustered t.

## Data

- Kalshi: WebSocket `orderbook_delta` (read-only, Ed25519 key).
- Polymarket US: public gateway `https://gateway.polymarket.us`, `/v1/markets/{slug}/book?_=<ns>`
  (cache-busted) polled every 250 ms per live window, one task each. `/bbo` is not used:
  it was seen frozen for 10 s while the book moved (likely the same CDN cache).

## Run

```
pk-pmus --env /etc/pk-pmus.env
```

See `pmus.example.env` and `pk-pmus.service`. Journal: `<state>/journal/YYYY-MM-DD.jsonl`
(`pu_window`, `pu_sec`, `pu_decision`, `pu_fill`, `pu_no_fill`, `pu_fill_probe`, `pu_close`,
`pu_resolution`, `pu_settle`, `pu_probe_settle`, `pu_book_err`, `pu_halt`, `pu_status`). The page is on `PK_PU_UI_ADDR`
(`/api/summary` is its JSON). `touch <state>/KILL` pauses entries; `touch <state>/ACK`
clears a loss halt.
