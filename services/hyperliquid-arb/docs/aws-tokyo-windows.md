# Host and broker: AWS Tokyo Windows box (Phase 0)

## Why Tokyo

Hyperliquid is the slow leg we quote, and its API and validators are in AWS ap-northeast-1. Hyperliquid itself recommends Tokyo. What the box location cannot fix is the distance to the broker's MT5 trade server:

| Broker trade server | Tokyo box → broker, one-way | What it costs |
|---|---|---|
| Equinix TY3/TY11 (Tokyo) | ~1–2 ms | nothing: one box is near both venues |
| Hong Kong | ~25 ms | small |
| London LD4/LD5, New York NY4 | ~110–120 ms | every hedge is late by this much; it adds to `hedge_ms` |

A hedge cannot start before the HL fill notice reaches the box. So with a far broker, the total MT5 tick → HL quote → fill → hedge path is about the same wherever the box sits. Tokyo still wins, because the HL side (order entry, cancels, the dead-man switch) stays at ~1–2 ms.

## Broker check: Pepperstone (2026-10-21)

The claim that Pepperstone runs a dedicated MT5 server in Equinix TY3, with Razor raw-spread accounts, is half right:

- **Razor account: true.** Spreads start from 0.0 pips, plus a per-lot commission: about USD 3.50 per side, $7 round turn, on MT5. Commissions on smaller trades are proportional.
- **Dedicated Tokyo (TY3) MT5 server: not supported by any evidence.**
  - No Pepperstone page names TY3. Its VPS page says only "22 key financial data centres".
  - VPS vendors' lists disagree. The more specific ones place Pepperstone's MT4/MT5 servers in Equinix LD4 (London) and NY4 (New York).
  - Measured here: the live MT5 host `mt5-1.pepperstone.com` → `mt5-global-live01.mq.pepperstone.com` is geo-routed DNS. It resolved to AWS access points in:
    - Cape Town (af-south-1) from Kigali;
    - **Hong Kong (ap-east-1) for a Tokyo client subnet**;
    - London (eu-west-2) for an Irish subnet;
    - Ashburn (us-east-1) for a US subnet.
  
  These are MetaQuotes access points that relay to the trade server. A Tokyo client is sent to Hong Kong, which means there is no Tokyo access point, and the trade server itself sits further away.
- **Rules that matter here:**
  - Pepperstone disallows latency-arbitrage EAs. Our hedges are not that, but get it confirmed in writing.
  - It caps EAs at about 30,000 server messages a day.
  - Its 24/7 perpetual CFDs cover indices, energies and metals, not FX.

**Conclusion:** Pepperstone qualifies on account type, but not as a Tokyo-hosted broker. Measure it with `hlarb mt5-roundtrip` from the Tokyo box. If its p50 order latency comes out near 200 ms or more, it is a far broker in the sense of the table above.

## Choosing the broker (Phase 0)

For each candidate (demo accounts are fine for latency):

1. Log the terminal into the account on the Tokyo box. Note the ping shown in the server list, and resolve the access point's location as above.
2. Start HlaPublisher and HlaExecutor, then run `hlarb mt5-roundtrip --symbol EURUSD --lots 0.01 --rounds 20`. This sends real orders: submit → fill per leg.
3. Check the account terms:
   - A-book / no last look;
   - no latency-arbitrage clause covering hedged EA positions;
   - netting mode available;
   - crypto CFD spreads (for BTC/ETH);
   - a reputable entity that accepts clients from Rwanda.

Choose the broker with the lowest `order_ms` p50 and p99 among those that pass the terms check.

## Choosing the AZ

From a Windows instance in each of apne1-az1, az2 and az4, run `hlarb probe --duration-seconds 600` and `mt5-roundtrip` against the chosen broker. Pick the AZ with the lowest p99 of `info_ws_post_rtt_ms` plus `order_ms`.

## Measurements (fill in)

```
date:
broker / server / access point:
AZ:
hlarb probe     info_ws_post_rtt_ms p50/p99:        bbo_age_ms p50/p99 (xyz:JPY, BTC):
mt5-roundtrip   order_ms p50/p99:
```
