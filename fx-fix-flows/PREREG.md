# FX fix flows — pre-registration, part 1

Written 2026-11-13, before any backtest output. Rules are frozen; changes go in a new part.

Sources:
- **KMW**: Krohn, Mueller & Whelan, *Foreign Exchange Fixings and Returns Around the Clock*,
  Journal of Finance 79(1), 2024 (BoC SWP 2021-48). G9 vs USD, 5-min mid quotes, 1999–2019.
  The USD rises before the Tokyo (09:55 JST), ECB (14:15 CET) and London (16:00 UK) fixes and
  falls after; ~2.5 bp per fix per day. Net of half the quoted spread the Tokyo reversal was
  positive for JPY, flat for GBP; with the full indicative spread most windows were negative.
- **BR**: Breedon & Ranaldo, *Intraday Patterns in FX Returns and Order Flow*, JMCB 45(5), 2013.
  A currency depreciates in its own trading hours (EBS 1997–2007). Their bid/ask strategy was
  net **negative** for USD/JPY and AUD/USD; only EUR/USD paid (no EURUSD data on hand).

## Data

HistData M1 bid bars for **USDJPY** and **AUDUSD**, 2020-01 → 2026-09 (fully after both
papers' samples). Stamps are New York time with DST. `p(t)` = close of the bar ending at
minute `t`, forward-filled up to 5 minutes; a leg with a missing price is skipped.

## Day and windows (KMW Note 1)

FX day D runs 17:00 ET on D−1 → 17:00 ET on D, for D = Monday–Friday. All local times with DST:

| leg | from | to | USD position |
|---|---|---|---|
| pre-T | 17:00 ET (D−1) | 09:55 Asia/Tokyo (D) | long |
| post-T | 09:55 Tokyo | 02:00 ET | short |
| pre-E | 02:00 ET | 14:15 Europe/Berlin | long |
| post-L | 16:00 Europe/London | 17:00 ET | short |

(The ECB→London window is dropped, as in KMW.) USDJPY: long USD = long the pair.
AUDUSD: long USD = short the pair.

Strategies: **T** = pre-T + post-T; **E** = pre-E + post-L; **W** = all four legs.

**BR sessions** (their Table 1): JPY 08:00–15:00 Asia/Tokyo; AUD 10:00–16:00 Australia/Sydney;
USD 08:00–16:00 ET, all on day D. Short the local currency in its own session and long it in
the USD session. **BR** = sum of the two session legs.

## Costs

Every leg is a round trip at **0.5 pip** (USDJPY 0.005, AUDUSD 0.00005); also reported at
0.3 and 1.0 pip. Returns are per day, in bp of the entry price, summed over legs.

## Tests (2020-01-02 → 2026-09-30, base cost)

One-sided t-test of daily net mean > 0.

- **Primary family (Holm 5%)**: T-USDJPY, T-AUDUSD.
- **Secondary family (Holm 5%)**: E-USDJPY, E-AUDUSD, W-USDJPY, W-AUDUSD, BR-USDJPY, BR-AUDUSD.

## Verdict

- **PASS**: at least one primary test survives Holm, and that pair's T is still net > 0 at 1.0 pip
  → shadow-trade T on that pair on MT5 demo.
- **SECONDARY ONLY**: no primary pass, at least one secondary survives Holm → paper-trade that
  rule only; new pre-registration before any change.
- **KILL**: neither.

Gross (zero-cost) leg means, per-year tables and the average intraday path are reported for
description; they are not gates.
