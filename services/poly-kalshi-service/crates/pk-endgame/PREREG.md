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

## Epoch 3 shadow rules (registered 2026-11-27, before deployment)

Epoch 3's traded rule is epoch 2's, unchanged (record-only paper halt, frozen-book
gates). The 2026-11-27 review (75 settled, +0.5¢/share, t 0.13) suggested four
single-setting changes. They are added as **shadow rules**: journaled, never
traded, and they do not start an epoch.

| Shadow | The one change | Why (from the 11-27 review, not a test) |
|---|---|---|
| `edge9` | `MIN_EDGE` 0.09 | ≥ 9¢ edges +10.3¢ (n 32), < 6¢ −5.3¢ (n 54) |
| `fav40` | `MIN_PX` 0.40 | asks ≥ 0.40 +14.5¢ (n 24); the model overrated cheaper sides |
| `sd11` | `SD_MULT_EARLY` 1.1 | the cheap-side overrating looks like an sd too wide early; refit's best 0–30 s mult was 1.1 |
| `spread10` | bid–ask spread of the side bought ≤ 0.10 (no bid = too wide) | spreads > 10¢ −9.3¢ (n 16) |

Each shadow fires at most once per coin-window, on the same evaluations,
freshness gates and Kalshi filter as the traded rule, and fills on paper against
the book 250 ms later (so fill selection is measured too). Shadows never count
toward open cost, P&L or the halt.

**Read once, at the epoch-3 H1 read** (≥ 150 settled traded positions over
≥ 3 days): for each shadow, its own settled net ¢/share after fees with
window-clustered t, counting only shadow entries made after deployment. A
shadow is a candidate for the next epoch's traded rule only if its net > 0 with
t ≥ 2 **and** it beats the traded rule over the same period. Four shadows are
read, so a lone t ≈ 2 is weak evidence: prefer the one with the largest t and
re-test it as epoch 4's traded rule before anything goes live.

**Not changed:** HYPE stays in (excluding coins mid-epoch is not allowed);
the Chainlink reconnect churn is an operations fix, not a rule.

## The weekly re-fit

`eg_refit.py` fits the sd multiplier per seconds-left band from the latest
`eg_sec` log, weekly (systemd timer), and writes `refit.json`. It **reports**
only. Adopting a new multiplier is a rule change: it starts a new epoch.
