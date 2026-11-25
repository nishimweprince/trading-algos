# pk-pmus

The poly-kalshi gap rule with **Polymarket US** as the venue, paper only.
Nothing is ever sent to either venue.

Polymarket US and Kalshi list the same 15-minute crypto Up/Down contracts on the
same CF Benchmarks indexes (BRTI, ETHUSD_RTI, SOLUSD_RTI, XRPUSD_RTI), so
Kalshi's mid is used as the fair value for Polymarket US.

## Rule

- Coins: BTC, ETH, SOL, XRP (`cpc-btc`, `cpceth-eth`, `cpcsol-sol`, `cpcxrp-xrp` 15m series).
- From 60 s after the open to 60 s before the close, buy the Polymarket US side that is
  3–15¢ cheaper than Kalshi's mid after the taker fee (Θ·p·(1−p), Θ = 0.0695).
- Kalshi book fresh (≤ 3 s) with a spread ≤ 3¢; Polymarket US quote ≤ 1.5 s old.
- One entry per coin per window, 10 shares, limit = ask + 1¢ capped at fair − fee − 1¢.
- Paper fill 150 ms after the decision against the Polymarket US book fetched then.
- Settles on Polymarket US's `/settlement`; Kalshi's result is journaled beside it.
- Caps: $50 open cost, $15 daily loss (halt until `ACK`).

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
