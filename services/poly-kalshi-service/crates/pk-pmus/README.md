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
- Polymarket US: public gateway `https://gateway.polymarket.us`, `/v1/markets/{slug}/book`
  polled every 250 ms per live window. `/bbo` is not used: it was seen frozen for 10 s
  while the book moved.

## Run

```
pk-pmus --env /etc/pk-pmus.env
```

See `pmus.example.env` and `pk-pmus.service`. Journal: `<state>/journal/YYYY-MM-DD.jsonl`
(`pu_window`, `pu_sec`, `pu_decision`, `pu_fill`, `pu_no_fill`, `pu_close`,
`pu_resolution`, `pu_settle`, `pu_halt`, `pu_status`). The page is on `PK_PU_UI_ADDR`
(`/api/summary` is its JSON). `touch <state>/KILL` pauses entries; `touch <state>/ACK`
clears a loss halt.
