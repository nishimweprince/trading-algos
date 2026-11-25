# pk-endgame pre-registration

Rules and tests are written down before the data that tests them exists. A
change to any rule below starts a new epoch; results are read per epoch.

## Epoch 1: baseline (2026-11-24 17:51 → 2026-11-25 ~11:30 UTC, Contabo, paper)

The first rule set (README at the time): model edge ≥ 4¢ after fee, asks
0.05–0.97, Chainlink-only model, ≥ 55 opening ticks. Result: 57 settled,
+2.42¢/share, s.e. 5.77, t 0.42, +$13.14. Our TWAP matched Polymarket in
251 of 252 windows.

What it taught, read from the per-second log (`eg_sec`) after the fact. These
findings are why epoch 2 exists; they are **not** evidence for epoch 2:

- In the last 15 s the winning side has no sellers in ~98% of seconds; 55 of
  57 entries fired 45–61 s before the close.
- Larger model edges did worse (45–61 s: edge ≥ 4¢ +4.9¢/share, ≥ 15¢
  −2.3¢): big disagreements with the book were model error.
- The model was overconfident outside the last 30 s; its sd fitted best ×1.5
  there (log-loss 0.108 → 0.099 at 30–61 s, 0.206 → 0.185 at 61–91 s), and
  ×1.0–1.25 inside it.
- Favourites (asks 0.70–0.97) lost 5–10¢/share; asks 0.30–0.50 made +16¢
  (t 1.75, n 30). Small samples.
- 91 windows were lost to < 55 opening ticks; 99.6% agreement says averaging
  the ticks that arrive is safe.

## Epoch 2 (registered 2026-11-25, before deployment)

**Rules** (all `PK_EG_*`, defaults in the code):

| | |
|---|---|
| Model sd ×1.5 while more than 30 s remain; ×1.0 inside the last 30 s | `SD_MULT_EARLY` 1.5, `SD_MULT_FROM_S` 30 |
| Model edge after fee between 4¢ and 12¢ (inclusive) | `MIN_EDGE` 0.04, `MAX_EDGE` 0.12 |
| Asks 0.05–0.70 | `MIN_PX`, `MAX_PX` 0.70 |
| ≥ 45 of 60 opening Chainlink seconds | `MIN_OPEN_TICKS` 45 |
| **Kalshi must agree:** Kalshi's mid for the same side (same window, BRTI 60 s averages) minus the Polymarket ask minus the fee ≥ 0; Kalshi book ≤ 3 s old with spread ≤ 0.05 | `KALSHI_FILTER` yes, `KALSHI_MIN_MARGIN` 0, `KALSHI_MAX_AGE_MS` 3000, `KALSHI_MAX_SPREAD` 0.05 |
| Unchanged: entries 60–2 s before the close, 10 shares, FAK at ask + 1¢ capped at 2¢ net edge, 250 ms paper latency, $50 open cost, −$15/day halt | |

**Primary test (H1), read once after ≥ 150 settled epoch-2 positions over ≥ 3
days including US hours:** net ¢/share after fees > 0 with window-clustered
t ≥ 2. Pass → candidate for live on the dedicated VM (not Contabo: France is
blocked). Fail → no live; the epoch-2 `eg_sec` log decides what to try next.

**Secondary reads (descriptive, not gates):** the same rule without the Kalshi
filter, scored from `eg_sec` on the same seconds (does Kalshi help?); fill
rate and shares kept after the delay; agreement of our TWAP with Polymarket
(must stay ≥ 99%); P&L by ask band and by seconds left.

**Not allowed mid-epoch:** changing any rule above, excluding coins, or
choosing a start time after seeing results. A code fix that changes no
decision (logging, page) does not start an epoch.

### Epoch 2 log, 2026-11-25

- 12:45 UTC review (6 settled, −$6.06; not a test read): 8 of 9 entries
  bought a side the model rated under 50% (asks 0.07–0.40); over every
  contested second Polymarket's own mid out-forecast the model and Kalshi
  (log-loss 30–61 s: market 0.408, Kalshi 0.421, model 0.457).
- ~13:00 UTC: spot recording added (Coinbase, Binance, and a spot-adjusted
  copy of the model in `eg_sec` and decisions). **Data only: no rule reads
  it, so epoch 2 continues.** Its purpose is the go/no-go for the strategy:
  if a spot-aware model cannot beat Polymarket's mid on log-loss, the
  endgame has no edge to sell.

## The weekly re-fit

`eg_refit.py` fits the sd multiplier per seconds-left band from the latest
`eg_sec` log, weekly (systemd timer), and writes `refit.json`. It **reports**
only. Adopting a new multiplier is a rule change: it starts a new epoch.
