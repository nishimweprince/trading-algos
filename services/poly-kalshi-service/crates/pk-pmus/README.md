# pk-pmus

The poly-kalshi gap rule with **Polymarket US** as the venue, paper only.
Nothing is ever sent to either venue.

Polymarket US and Kalshi list the same 15-minute crypto Up/Down contracts on the
same CF Benchmarks indexes (BRTI, ETHUSD_RTI, SOLUSD_RTI, XRPUSD_RTI), so
Kalshi's mid is used as the fair value for Polymarket US.

## Rule

- Coins: BTC, ETH, SOL, XRP (`cpc-btc`, `cpceth-eth`, `cpcsol-sol`, `cpcxrp-xrp` 15m series).
- From 60 s after the open to 60 s before the close, buy the Polymarket US side that is
  5–15¢ cheaper than Kalshi's mid after the taker fee (Θ·p·(1−p), Θ = the market's `feeCoefficient`, 0.11).
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

## Epoch 4: the market's fee (2026-11-26)

The taker fee is each market's own `feeCoefficient` (0.11 on the 15m crypto markets),
not the 0.0695 used before. It applies to the gap, the limit cap, fills, probes and the
sizing rules. `PK_PU_FEE_THETA` (default 0.11) is used only for a market that does not
report one. `pu_window.fee_coef` and `pu_decision.fee_theta` record the rate used. At
0.11 the fee is about 2.7¢ at p = 0.5, against 1.7¢ before, so fewer gaps clear 5¢.
Epoch 3 (about 1.5 h at Θ = 0.0695) is superseded.

## 20 and 30 s exit probes (2026-11-28, within epoch 7)

`PK_PU_EXIT_DELAYS_MS` now defaults to 1, 2, 5, 10, 20 and 30 s. At 191 settled fills the
exit's net per share was still rising at 10 s (+2.71¢, t 3.73). Epoch 6's once-a-second
data suggested a peak somewhere between 10 and 30 s. The new delays locate that peak for
the epoch 8 pre-registration. They are secondary: the 2 s primary test and its 300-fill
count are unchanged. Entries stop 60 s before the close, so a 30 s exit always lands
inside the window.

## Latency telemetry (2026-11-28, within epoch 7)

No Polymarket US API key yet, so there are no orders to time. The bot measures what it can
(`src/lat.rs`; the page's "Latency" section; `pu_latency` journaled every minute):
- **`gw_rtt`:** a gateway book request on the warm HTTP/2 polling connection, from send to
  parsed body, for cache misses only.
- **`gw_server`:** Polymarket US's own `x-pm-server-latency` for that request.
- **`gw_edge_origin`:** `gw_rtt` minus `gw_server`, the trip from here through the
  Cloudflare edge to the origin and back.
- **`k_lock_wait`, `k_process`:** a Kalshi frame waiting for the engine lock, then being
  applied and evaluated.
- **`timer_lag`:** how late a 10 ms timer fires. This is the noisy-neighbour signal.

The page estimates the live arrival time as processing + `gw_edge_origin`/2 + server time,
and reads the fill rate at that arrival off the measured arrival-delay curve. It flags a
dedicated-CPU plan only when `timer_lag` p99 exceeds 3 ms (tokio timers tick in 1 ms steps, so about 1 ms late is the floor) or `k_process` p99 exceeds 1 ms.

**Origin probe, 2026-11-28.** These are Globalping HTTP probes on cache-busted gateway
requests; first byte is measured after TLS.

| Probes from | Cloudflare edge | First byte |
|---|---|---|
| Ashburn | IAD | 8–18 ms (edge ↔ origin about 2–10 ms) |
| Columbus | CMH, PIT | 18–30 ms |
| Chicago | ORD | 21–35 ms |
| New York | EWR | 30–62 ms |
| Dallas | DFW | 37–44 ms |

So the Polymarket US origin is in or near Ashburn, and this host is already in the right
metro. Within Ashburn, by network over 8 samples each:

| Network | Median first byte | Minimum |
|---|---|---|
| Amazon | 13 ms | 8 ms |
| Hetzner | 17.5 ms | 9 ms |
| OVH | 12.5 ms | 10 ms |
| Oracle | 14.5 ms | 9 ms |

An AWS us-east-1 host might save 0–4 ms. That's within noise for these samples; re-test
with a real order ack.

Paper still fills at 150 ms (`PK_PU_PAPER_LATENCY_MS`) so that epoch 7's exit study runs
under one setting. Set it to the measured order ack p95 once orders exist.

## Kelly caps raised (2026-11-28, within epoch 7)

The Kelly rules now have their own dollar caps: `PK_PU_KELLY_TRADE_USD` ($50 per trade) and
`PK_PU_KELLY_WINDOW_USD` ($100 per window across coins). Before this they shared the
bankroll caps of $20 and $50. The other rules keep 2% and 5% of the bankroll.

A replay of the 326 epoch 6–7 fills against their own books shows what this changes:
- **At $20 / $50,** quarter Kelly hit the cap on 140 fills and book depth on 114.
- **At $50 / $100,** the cap binds on 5 and depth on 145.
- **At $100 / $200,** the results are identical to an uncapped run.

So above about $50, depth at the limit sets the Kelly size. Each fill journals
`kelly_cap`, and the page's Kelly rows count only positions sized under the current cap.
Only what-if columns change, so the epoch stays at 7 and its exit pre-registration count
is unaffected.

## Epoch 7: quick exit against holding (2026-11-27)

The epoch 6 review (25 h, 321 decisions, 130 fills) found that holding to settlement earns
nothing (−0.66¢/share, t −0.16), but 320 of 325 decisions are triggered by a Kalshi update
and Polymarket US then moves about 8.6¢ toward Kalshi within 2 s, while Kalshi stays where
it was. Selling back at the bid 2 s after the fill (fee on both legs) would have netted
+1.8¢/share, t 4.2, on the actual fills. That figure is in-sample, from top-of-book
once-a-second snapshots. Epoch 7 measures the exit properly, without trading it:

- **`pu_exit_probe`.** For every fill, the book is fetched `PK_PU_EXIT_DELAYS_MS`
  (1, 2, 5 and 10 s) after the fill, and the whole position is walked into the bid level by
  level. Up sells into Yes bids; Down sells No at 1 − the Yes offer. The record holds the
  shares sold, the proceeds, the exit fee, and the P&L on the sold shares.
- **`pu_exit_settle`.** At settlement, each probe's P&L is the exit proceeds minus fees,
  plus any unsold remainder held to settlement, minus the entry cost. The record pairs this
  with `hold_pnl` for the same position and with `diff` (exit − hold).
- **Page.** A "Quick exit against holding" table, by delay: fills, fully-sold rate, shares
  sold, exit and hold net per share, and exit − hold with a paired, window-clustered t.

**Pre-registered (fixed before any epoch 7 data):**
- **Primary:** the 2 s exit nets more than 0 per share, window-clustered t ≥ 2.0, over 300
  settled epoch 7 fills. The page shows collecting / pass / fail.
- **Before 300:** no change to the trading rule.
- **Secondary, reported but not gating:** the 1, 5 and 10 s exits, and exit − hold.
- **If it passes:** the next step is a sell order after the fill, which needs a Polymarket
  US account, and a latency measurement of real order entry.

## Epoch 6: per dollar, not per share (2026-11-26)

A cent of edge is worth c dollars of stake: a 5¢ gap is 50% of a $0.10 share and 5.6% of
a $0.90 one, which risks $0.90 to make $0.10. Epochs 1–5 gated, tiered and scored in ¢ per
share. Epoch 6 adds, without changing what is traded (5¢ gate, flat 10):

- **Per-dollar gate, as a shadow (`pu_rpd`, `pu_rpd_settle`).** The first gap per
  coin-window with edge ÷ cost ≥ `PK_PU_RPD_MIN` (10%), edge between `min_net_edge` (1¢) and
  `max_edge` (15¢), costed at ask + fee for one share. It keeps looking after the 5¢ rule's
  entry, so both gates are compared on whole windows. `passes_5c` marks gaps the 5¢ rule
  would also take. `pu_shadow_settle` now carries `cost`; `pu_decision` carries `cost` and
  `rpd`.
- **Dollar sizing rules.** `usd_flat`: `PK_PU_STAKE_USD` ($5) ÷ c shares. `usd_tiers`: 1×,
  2×, 3× the stake at < 10%, 10–20%, ≥ 20% return per dollar. Same caps as the other rules.
- **Per-dollar scoring.** Results show return per dollar (P&L ÷ cost); new tables "Return
  per dollar by price" (settled positions by cost per share) and "Gate: 5¢ against per
  dollar"; "Does the gap pay?" adds price bands and predicted/realized per dollar.
- `PK_PU_CAL_EPOCHS` defaults to 4,5,6.

## Epoch 5: does the gap pay? (2026-11-26)

Every Kelly rule trusted Kalshi's mid as the win probability, the same number that fires
the trade. Epoch 5 adds two measured rules (both quarter Kelly) and a page table:

- **Calibrated Kelly (`kelly_cal`).** q_cal = c + β·(q − c), with β fitted through the origin
  on settled shadows: x = the gap to Kalshi's mid after fee, r = won − cost. β ≈ 1 means
  Kalshi is right; β ≈ 0 means the gap is noise. Shadows (one per coin-window, from 3¢) are
  used rather than fills because they are several times as many and never double-count a
  window. β is shrunk toward `PK_PU_KELLY_SHRINK` (0.5) by `PK_PU_CAL_PRIOR_N` (20)
  pseudo-shadows at a 6¢ gap and clamped to [0, 1]. Only shadows settled before a fill count
  (no look-ahead). At start β is seeded from the journal's shadows of `PK_PU_CAL_EPOCHS`
  (4,5: the first epochs on uncached books at the 0.11 fee); each `pu_fill.cal` records the
  β, n and q used.
- **Near-side Kelly (`kelly_c`).** q is Kalshi's near side, the Yes bid for Up and 1 − the
  Yes ask for Down, and the gap to it halves every `PK_PU_CONS_AGE_HALF_MS` (10 s) of
  Kalshi book age (`pu_decision.q_cons`, `kalshi_book_age_ms`). A wide or stale Kalshi book
  now counts for less.
- **"Does the gap pay?"** Settled shadows by predicted gap and by seconds left: predicted
  gap to the mid and to the near side, realized gap with a window-clustered s.e., and β for
  both. `pu_shadow` and `pu_shadow_settle` carry `x_cons`; for epoch 4 shadows it is
  approximated as the gap minus half Kalshi's spread.

Paper still trades a flat 10; nothing here changes the decision or the fill.

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
