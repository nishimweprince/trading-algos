I'll redo the analysis around Hyperliquid's FX perps, mainly JPY and AUD, and compare IC Markets, Pepperstone and other raw-spread brokers suited to high-frequency trading.

You're right that the report leaned too heavily on gold. Narrowing to FX changes the picture in three ways.

1. **AUD isn't tradable on Hyperliquid at all.** Its only FX perps are trade.xyz's EUR, JPY and GBP (plus KRW).
2. **"Less liquid" is half right.** Hyperliquid's FX books are thin, but the edge isn't in price gaps.
3. **The real opportunity is a funding-versus-swap mismatch**, and JPY is the clearest case.

## What Hyperliquid actually lists in FX

As of October 14, 2026, Hyperliquid lists 292 perp markets: 178 native crypto perps and 114 HIP-3 markets deployed by trade.xyz. Forex is only 3 of those. The FX lineup launched in May 2026 with EUR, JPY, KRW and the DXY dollar index. GBP was added later. There is no AUD, CHF or CAD. BitMEX launched FX perps in April 2026 that include AUD/USD, USD/CHF and USD/CAD, so an AUD version of this idea would have to run MT5 against BitMEX, not Hyperliquid.

Contract details for JPY:
- It is quoted like MT5's USDJPY (yen per dollar), with 50x max leverage, a ±2% weekend price band that can move once, isolated margin only, and real-market pricing from Sunday 5PM ET to Friday 5PM ET.
- Its reference price is pushed on-chain only about every 3 seconds.
- On fees, JPY, EUR and GBP appear to be on Hyperliquid's discount tier. GBP shows a standard 9.0 bps taker / 3.0 bps maker rate and a growth-tier rate of 0.9 / 0.30 bps. Check the `growthMode` flag in the API before trading.

## Why price arbitrage on FX is thin, and carry isn't

A 30-day study of every trade.xyz market up to 22 Sept 2026 found FX to be the tightest-tracking group, with a median of 3.2 bps from the reference price. It also recorded the funding rates, which are where the gap is:

| Market | Gap vs reference | Wobble (30d) | Funding 24h / 7d (annualized) | 24h volume |
|---|---|---|---|---|
| JPY | −5.2 bps | 3.8 bps | −19.6% / −12.3% | $3.3M |
| EUR | +4.8 bps | 2.7 bps | +17.1% / +8.1% | $1.7M |
| GBP | +2.3 bps | 3.2 bps | +1.3% / −4.2% | $159K |



The mismatch is structural:
- **MT5 swap tracks interest rates.** The Fed is at 3.75–4.00%. The BOJ is at 1.25%, effective September 24. That is roughly a 2.6-point gap, so long USDJPY earns swap and short USDJPY pays it.
- **Hyperliquid funding ignores interest rates.** For xyz contracts, the fixed component that longs pay was halved to about 5.5% a year. Everything else depends on whether the perp trades above or below its reference.
- **Negative funding means longs get paid.** USDJPY on Hyperliquid has traded below its reference, so funding has been negative and longs collect it. A separate snapshot showed the same: $30.89M open interest, funding of −0.0015% per hour (−13.55% annualized), and the perp 5.2 bps below its reference.

**The trade:**
- **Legs:** go long xyz:JPY on Hyperliquid and collect funding. Short USDJPY on MT5 and pay swap.
- **Swap cost on the MT5 leg:** IC Markets Raw quotes USDJPY swap at +9.794 points long and −16.096 points short per lot per night. Another regional page shows +8.626 / −17.464. That works out to about **−3.7 to −4.1% a year** on the short.
- **Net carry:** if funding averages around −12%, you net roughly **+8% a year**, before about 2.5–3 bps of round-trip costs, which one day of carry covers.
- **EUR mirror:** short xyz:EUR and collect about 8–17%. Hedge with a long EURUSD on MT5, which at IC costs −6.25 points per lot per night (about −2% a year).
- **GBP:** no carry edge.

This is the strongest opportunity because the MT5 leg looks like an ordinary multi-day position. That avoids the latency-arbitrage clauses brokers enforce.

**The catch:** that funding is driven by flows, not structure. The figures above are two snapshots, from May and from 22 September. Before committing capital, pull 90+ days of `fundingHistory` for xyz:JPY and xyz:EUR.

## Other FX ideas, ranked

| Rank | Idea | Edge | Verdict |
|---|---|---|---|
| 1 | Funding-vs-swap carry (JPY, then EUR) | ~6–8%/yr if funding persists | **Most realistic.** Capacity is limited by ~$30M open interest. |
| 2 | Gap mean-reversion: post maker orders on Hyperliquid, hedge on MT5 | A few bps per trade | **Testable.** Costs ~1.5–2.5 bps round trip against a 3–4 bps typical wobble. Thin books mean small size. |
| 3 | Event dislocations (FOMC Oct 27–28, BOJ Oct 30, Ministry of Finance intervention) | 10–50+ bps, rarely | **Worth automating.** Hyperliquid quotes can be up to ~3s stale and its makers pull liquidity. You trade against Hyperliquid's book, not the broker. |
| 4 | Weekend JPY price as a Monday-gap signal | Directional | A signal, not arbitrage. MT5 is closed and Pepperstone has no 24/7 FX CFDs. |
| 5 | Synthetic EURJPY/GBPJPY from two Hyperliquid legs vs the MT5 cross | Likely negative | Fees on two Hyperliquid legs plus wider spreads on MT5 crosses. Research only. |
| 6 | Latency arbitrage against the MT5 quote | — | **Don't.** Broker terms ban it (below). |



## Brokers: raw / zero-pip accounts

On USDJPY at about 157, a $7 round-trip commission is roughly 0.7 bps per lot. The MT5 leg is therefore 0.5–1.0 bps all-in at any of these brokers. The bigger difference between them is the **short-USDJPY swap markup**, because that is the main cost of trade #1.

| Broker (raw account) | Commission per side | Notes |
|---|---|---|
| IC Markets Raw | $3.50 per lot | Swap published (above). Terms let it reclaim latency-arbitrage profits at any time. |
| Pepperstone Razor | $3.50 per lot (USD account); USDJPY averaged 0.4 pips (July 2024 data) | Swap is based on the tom-next rate. Latency-arbitrage EAs aren't allowed, and EAs should stay under 30,000 server messages a day. Its 24/7 perpetual CFDs cover indices, energies and metals, not FX. |
| Fusion Markets Zero | $2.25, with a New York server | Cheapest commission. |
| Tickmill Raw | $3 | |
| Global Prime Raw | $3.50, routed from cross-connected NY4 servers | True ECN. |
| FP Markets Raw | ~$6 round trip; explicitly permits scalping and algorithmic high-frequency trading | |

**What to check before opening an account:**
- A-book/STP execution without last-look rejections.
- The **actual short-USDJPY and long-EURUSD swap** shown in the MT5 symbol specification. Compare it with the ~2.6% rate differential to see the markup.
- Avoid swap-free (Islamic) accounts for this trade. Brokers treat carry harvesting through them as abuse.
- Clause wording. ThinkMarkets' terms, which went to the English High Court, ban arbitrage and price-latency trading and let it revoke such transactions. Get written confirmation that a hedged, multi-day position is fine.

## Risks specific to FX

- **Funding can flip.** Exit when 24h annualized funding drops below your swap cost.
- **Isolated margin only.** A Ministry of Finance intervention can move USDJPY 2–4% in minutes. Run the Hyperliquid leg at 3–5x, not 50x.
- **Weekends.** On weekends Hyperliquid JPY keeps trading within its band while MT5 is closed. Your hedge is frozen but still nets out.
- **Thin markets.** GBP's $159K/day can't absorb meaningful size.
- **Daily rollover.** MT5 raw spreads blow out around 5pm ET. Don't trade the MT5 leg then.

## Next step

Run a data collector for 2–4 weeks, deliberately spanning next week's FOMC and BOJ meetings. It should:
- pull Hyperliquid `fundingHistory` and `metaAndAssetCtxs` for xyz:JPY, EUR and GBP, using the `dex:"xyz"` parameter;
- log MT5 ticks and swap specs from 2–3 of the brokers above;
- compute realized carry net of each broker's swap.

That one dataset settles whether ideas 1–3 hold up. Want me to scaffold that collector in your Python/NestJS stack, or turn this into a doc alongside the earlier report?

Sources:
- [trade.xyz Specification Index](https://docs.trade.xyz/consolidated-resources/specification-index)
- [trade.xyz Perp Mechanics Overview](https://docs.trade.xyz/perp-mechanics/overview)
- [Hyperliquid Guide – HIP-3 tracking error study](https://hyperliquidguide.com/guides/trading/stock-perps-tracking-error)
- [Hyperliquid Guide – JPY market](https://hyperliquidguide.com/markets/xyz/jpy)
- [Hyperliquid Guide – FX perpetuals](https://hyperliquidguide.com/ecosystem/hyperliquid-fx-perpetuals)
- [Hyperliquid Guide – Markets list](https://hyperliquidguide.com/markets)
- [DefiLlama – xyz:GBP](https://defillama.com/rwa/asset/xyz%3AGBP)
- [ForkLog – TradeXYZ explained](https://forklog.com/en/?p=104909)
- [BitMEX FX perps launch](https://www.aap.com.au/aapreleases/globenewswire1001178730/)
- [Nation Thailand – BOJ to 1.25%](https://www.nationthailand.com/news/world/40071184)
- [Admiral Markets – Fed raises to 3.75–4.00%](https://admiralmarkets.com/analytics/traders-blog/fed-raised-interest-rates)
- [IC Markets spreads & swaps](https://www.icmarkets.eu/en/trading-pricing/spreads-and-swaps)
- [IC Markets trading costs](https://www.icmarkets.eu/pt/trading-pricing/trading-costs)
- [Pepperstone UK costs & charges (July 2026)](https://eu-assets.contentstack.com/v3/assets/bltd74737b907a31bbb/bltc19314db598b7ab0/UK_Legal_Costs_and_Charges.pdf)
- [Pepperstone pricing](https://next.stg.pretrade.pepperstone.com/en-af/ways-to-trade/pricing/)
- [Pepperstone – EA guidance](https://pepperstone.com/en-gb/insights/guides/automated-trading/what-should-i-be-aware-of-when-using-an-expert-advisor-with-a-broker)
- [QuantVPS – broker comparison](https://www.quantvps.com/blog/top-rated-forex-brokers)
- [Investingoal – HFT brokers](https://investingoal.com/forex/broker/hft/)
- [TF Global Markets v Tan (EWHC 2020)](https://caselaw.nationalarchives.gov.uk/ewhc/admin/2020/3178)
- [FinanceCalendar – BOJ October 2026](https://www.financecalendar.com/?p=1256)