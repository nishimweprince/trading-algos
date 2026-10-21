# MT5 ↔ Hyperliquid Arbitrage: What Is Real, What Is Illusory, and How to Test It (October 2026)

Classic MT5-vs-Hyperliquid price arbitrage on gold is mostly illusory for a solo developer. Hyperliquid's gold perp is not fee-discounted (0.09% taker), its normal-hours price sits only a few basis points from its oracle, and MT5 brokers contractually ban latency arbitrage. The realistic opportunities are narrower. They use Hyperliquid's 24/7 HIP-3 markets as the venue where mispricing happens, and MT5 only as a fairly priced, slow hedge: weekend and perpetual-CFD pairs, dislocation capture, and conditional funding/swap carry.

## TL;DR

- **Latency/price arbitrage between MT5 XAUUSD and Hyperliquid xyz:GOLD doesn't work.** A taker round trip on both legs costs about 19 bps, but the median mark-to-oracle basis on trade.xyz markets is 4.31 bps and the p99 is about 18 bps. IC Markets' terms also let it void latency-arbitrage profits "at any time during our trading relationship."
- **Gold carry is roughly zero after costs.** Shorting the Hyperliquid gold perp earns funding of about 5.5%/yr, while a long MT5 XAUUSD position pays about 4.8–6%/yr in swap. Crypto carry is usually negative, with one exception: when Hyperliquid funding turns negative, a long Hyperliquid perp plus a short broker CFD can earn on both legs.
- **The best opportunities are structural, not speed-based:**
  - (1) Weekend pairs between broker 24/7 "perpetual CFDs" on MT5 (Pepperstone GOLD-PERP, NAS100-PERP, SP500-PERP) and trade.xyz perps.
  - (2) Catching rare Hyperliquid flash dislocations, such as gold's roughly $100 drop on 4 July 2026, with an MT5 hedge.
  - (3) Using Hyperliquid's weekend price as a strong signal for the Monday MT5 open. The direction was right in 94.9% of weekend moves larger than 100 bps.
  - Validate each one with data collection before risking more than about $10–25k.

## Key Findings

### 1. Instrument overlap (as of Oct 2026)

Almost all non-crypto overlap runs through one HIP-3 deployer, **trade.xyz** (prefix `xyz:`). It listed 110 active markets on 3 October 2026: 76 stocks, 9 commodities, 4 equity indices, 17 ETFs, 3 FX pairs and 1 pre-IPO market. It also carries more than 90% of HIP-3 open interest; on an average September day it held 98.1% of HIP-3 OI. Its September 2026 volume was $59.38B, 23.9% of all Hyperliquid perp volume. Weekend volume still averaged $541.4M a day.

| Asset class | MT5 CFD (typical) | Hyperliquid market | Sept-2026 volume / OI | Max lev. / weekend bound |
|---|---|---|---|---|
| Gold | XAUUSD | xyz:GOLD (references XAU/USD spot) | $1.76B Sept volume; ~$166.5M OI (ARX snapshot) | 25x / ±4%, 2 resets |
| Silver | XAGUSD | xyz:SILVER | $2.85B | 25x / ±4% |
| WTI / Brent | USOIL/UKOIL | xyz:CL, xyz:BRENTOIL | $7.13B / $4.02B | 20x / ±5% |
| S&P 500 | US500/SPX500 | xyz:SP500 (licensed from S&P DJI, launched 18 Mar 2026) | $5.47B; avg OI $382.9M (largest) | 50x / ±2% |
| Nasdaq-100 style | NAS100/USTEC | xyz:XYZ100 (a custom 100-stock index, *not* the official NDX) | $5.66B | 30x / ±3.5% |
| FX | EURUSD, USDJPY, GBPUSD | xyz EUR, JPY, GBP (plus KRW, DXY listed) | 0.5% of xyz volume (thin) | up to 50x |
| US/Asian single stocks | Share CFDs (TSLA, NVDA, MU, etc.) | 76 xyz stock perps, incl. SK hynix, Samsung | SKHX $5.33B | mostly 10x |
| BTC, ETH, SOL, etc. | BTCUSD, ETHUSD crypto CFDs | Native Hyperliquid validator perps | Deepest books on HL | up to 40–50x |

Other HIP-3 deployers are small:
- Kinetiq Markets, HyENA, Paragon and EntropyIO together held only tens of millions in OI at end-August, against trade.xyz's $3.57B.
- Felix Perps wound down in 2026.
- Ventuals closed its OpenAI/Anthropic markets in June.

Headline HIP-3 OI numbers depend on methodology. Grayscale reported a $3.2B peak in June 2026, and a Hyperliquid API snapshot showed $3.60B on 31 August. A fills-based estimate gave $1.93B on 19 October 2026. Treat the most recent figures as uncertain, and pull `metaAndAssetCtxs` with `"dex":"xyz"` yourself.

### 2. The fee asymmetry that decides gold

- **xyz:GOLD is not in growth mode.** On 3 October 2026, growth mode was on for 105 of 110 xyz markets. The five without it are gold and four crypto-linked stocks. Gold therefore pays standard HIP-3 fees of **0.090% taker / 0.030% maker** at base tier, and Hyperliquid's own trade screen shows exactly "0.0900% / 0.0300%" for xyz:GOLD.
- **Growth-mode markets cost 0.009% taker / 0.003% maker at base tier.** Hyperliquid's docs say growth mode cuts all-in fees by at least 90%, putting the baseline taker rate between 0.0045% and 0.009%, with a 30-day cooldown per asset. These include SP500, XYZ100, SILVER and CL. That is 10x cheaper than gold, and a fifth of what a taker pays on Hyperliquid's native BTC perp.
- **Native crypto perps cost 0.045% taker / 0.015% maker** at base tier. Fees fall to 0.024% taker and 0% maker above $7B of 14-day volume.
- **MT5 ECN gold is nearly free by comparison.** At IC, a $7 round-turn commission works out to $0.07/oz. Add a raw spread of $0.03–0.12 and the all-in cost is about **0.4–0.5 bps** at ~$4,125 gold.
- **Fees are unstable.** AMBCrypto (6 Aug 2026) reported founder Jeff Yan saying deployers could raise fees on specific listed assets by up to 3x, not as a blanket increase, with "no shared timeline." Another says growth mode stays, with per-market fee scales; per-asset configuration has applied since August 2026. Re-check fees before every backtest.

### 3. Weekend pricing mechanics (the core of any HIP-3 vs MT5 idea)

Each trade.xyz market has three prices:
- **Oracle:** the price funding is paid against.
- **Mark:** the median of three inputs (oracle; oracle plus the recent book basis; and the median of best bid, best ask and last trade). Liquidations run on the mark.
- **External:** the last real-market price.

When the underlying closes, the external price freezes at the Friday close. The oracle then tracks trade.xyz's own order book, smoothed with a 30-minute time constant.

**Discovery bounds** cap how far the mark can drift from a reference price. The bound is ±(1/max leverage), so ±4% for GOLD. When the oracle reaches 90% of a bound, the reference re-anchors. After the configured number of resets the bound becomes a hard cap, and a position whose liquidation price lies outside the active band can't be liquidated. For a 20x market with two resets, the maximum weekend range is about +15.8% / −14.3%. When external pricing resumes, the oracle snaps back to the live external price on the next tick.

**Evidence on weekend accuracy:**
- **Blockworks/0xArchive study (Aug 2026), 614 market-weekends:**
  - The final weekend price was closer to the reopen than Friday's close 70.7% of the time.
  - Median error fell from 113.3 to 53.4 bps.
  - Direction was correct in 94.9% of weekend moves larger than 100 bps.
  - Implied moves carried into realized moves at about 0.83 to 1.
  - At the reopen, 84% of markets were back within 10 bps of the oracle within 5 minutes, and 89% within 15.
  - The research was funded by the Hyperliquid Policy Center, so treat it as favourable to Hyperliquid.
- **Weekend liquidity:** spreads held (1.43 bps median), but depth fell to 31–50% of weekday levels. The p99 fill cost widened from 23.7 to 41.2 bps.
- **Earlier, smaller samples were weaker.** One sample of 191 observations showed only 50.7% accuracy and a 0.4 bps improvement. The Hormuz-period sample of 199 observations showed 78.4% and a slope of 0.816. Performance is regime-dependent.
- **Bounds can trap price discovery.** On the second Hormuz weekend (March 2026), xyz:CL was pinned at its +5% ceiling of $95.83, while CME reopened at $106.89, an 11.54% gap. On the same weekend, Kinetiq's crude market reached 110.64% of its Friday reference. Two HIP-3 deployers can disagree by more than 5% on the same asset.

## Details: Arbitrage Types, Mechanics and Edge Estimates

### A. Price/latency arbitrage (MT5 quote vs Hyperliquid mid/mark) — **Illusory for gold, marginal for indices**

- **How it would work.** Watch MT5 XAUUSD against xyz:GOLD, buy the cheap venue and sell the rich one, then unwind on convergence.
- **Why it fails on gold.** A taker round trip on both legs costs about 2×9 + 2×0.5 ≈ **19 bps (~$8.30/oz at $4,365)**. Using Hyperliquid maker orders cuts that to about **7 bps (~$3)**. During open sessions, the median mark-to-oracle basis on trade.xyz markets is **4.31 bps**, with a median market's p99 of about **12.6 bps** and a pooled p99 of **18.46 bps**. A recent xyz:GOLD snapshot showed a book spread of 0.25 bps and a mark/oracle basis of +2.23 bps. Professional market makers already hedge these markets off-chain through brokers. The gap rarely exceeds costs, and when it does you're competing with them.
- **Data latency is against you.** After June 2026 changes, Hyperliquid's standard public order-book feed updates about every 5 seconds. The median block-to-server latency even from Tokyo is around 285–304 ms on the public API.
- **Broker risk is decisive.** IC Markets (EU) Terms v5.0 (June 2026) define "Abusive Trading" to include "arbitrage… a combination of faster/slower feeds… lag trading, usage of server latency… latency arbitrage, stale-price exploitation." Clause 12.5 lets the broker "obtain from your Account any historic trading profits… at any time during our trading relationship."
- **Indices are only marginal.** On growth-mode markets (SP500, XYZ100) the Hyperliquid leg costs about 1–2 bps round trip. The binding constraints become the MT5 index CFD spread and a modelled basis. MT5 US500 is often priced off futures, while xyz index oracles blend cash indices with futures stripped of financing and dividends. You must model a drifting fair basis, not assume parity.
- **Verdict.** Edge is under 0 after costs on gold. On indices, edge only exists in tails and is probably broker-blocked. Do not build this.

### B. Funding vs swap carry (basis trade) — **≈ zero on gold; conditional on crypto**

**Gold.** trade.xyz halves Hyperliquid's funding interest component, so baseline funding is about **5.5%/yr**, paid by longs to shorts when the premium is near zero. A recent snapshot showed +0.0006%/hour, with a 7-day mean of +0.0008%/hour (about 5–7%/yr). On MT5:

| Broker | XAUUSD long swap (per lot/night) | Short swap (per lot/night) | Approx. annualized at ~$4.1–4.4k gold |
|---|---|---|---|
| Afterprime (15 Oct 2026) | −$72.26 | +$28.29 | long ≈ −6.0%, short ≈ +2.4% |
| IC Markets (Myfxbook) | −$53.76 | +$36.93 | long ≈ −4.8%, short ≈ +3.3% |
| RoboForex | −$60 | +$19 | long ≈ −5.3%, short ≈ +1.7% |

- **Short perp + long MT5:** +5.5% − (4.8 to 6.0%) ≈ **−0.5% to +0.7%/yr** before trading fees. A single taker round trip on xyz:GOLD (~18 bps) wipes out roughly 3–4 months of that best case.
- **Long perp + short MT5:** −5.5% + 1.7 to 3.3% = **negative.**
- **Verdict for gold:** not a trade. It is useful only as a cheap hedge for positions you already want.

**Crypto.** Hyperliquid's native baseline funding is about 11%/yr, which xyz halves. Broker crypto CFD financing is usually much heavier on longs:
- Pepperstone's published example charges a long BTCUSD **0.05556%/day (~20%/yr)** and credits a short **0.02083%/day (~7.6%/yr)**.
- IC applies about −20 points on longs and 0 on shorts across most crypto CFDs.
- Brokers charge a triple swap on Fridays for the weekend.

That gives three cases:
- **Long CFD + short Hyperliquid perp** loses money unless Hyperliquid funding stays above about 20%/yr, which only happens in frenzied markets.
- **Short CFD + long Hyperliquid perp** in a normal market means paying about 11% and receiving about 7.6% where a short credit exists. That is negative.
- **The conditional opportunity:** when Hyperliquid BTC/ETH funding turns **negative** (longs get paid), a long Hyperliquid perp plus a short CFD at a broker that *credits* shorts earns on **both** legs. This is episodic, typically after liquidation cascades. Verify your own broker's current short swap, since Pepperstone's example comes from a cost-disclosure PDF and rates change.

Hold for days to weeks while the funding regime lasts. Size around $5–20k notional per leg.

### C. Weekend / off-hours — **Signal value is real; true arbitrage needs a broker that is also open**

- **Plain weekend gap trade (MT5 closed).** There is no hedge leg, so this is directional speculation on Hyperliquid, not arbitrage. The 0.83 implied-to-realized ratio suggests Hyperliquid weekend moves overshoot by about 17% on average. Fading large weekend moves on Hyperliquid before the reopen is a testable *statistical* idea, but the risks are large:
  - weekend depth is only 31–50% of weekday levels;
  - bounds can pin price, as with CL on the Hormuz weekend;
  - the Monday jump can blow through your stops.
- **Monday-open convergence.** When MT5 reopens, its first quotes and the Hyperliquid oracle converge within minutes (84% within 10 bps in 5 minutes). Professional market makers are already in that race. The edge is small and fragile.
- **Using it as a signal for your scalping.** This is the most practical value for XAUUSD intraday trading. Before the Sunday-night open, read xyz:GOLD's distance from Friday's close. Direction was right 94.9% of the time when the weekend move exceeded 100 bps, so it tells you which side the open is likely to gap. It is a bias filter, not risk-free profit.
- **The genuine two-venue weekend pair is new in 2026.** Pepperstone now offers 24/7 **perpetual CFDs on MT5** (GOLD-PERP, NAS100-PERP, SP500-PERP, WTI, BRENT and several stocks):
  - Spreads start from 0.3 points plus funding, and commission applies on gold perps.
  - Funding is a fixed weekly rate applied daily as swap, "derived from the peer-to-peer funding rate mechanism," with no broker markup.
  - Pepperstone does not disclose the reference venue ("reference a synthetic market or price").
  - BlackBull lists XAUUSD-PERP and was quoted about $4–12 below spot XAUUSD in snapshots.
  - GCC Brokers' MT5 XAUUSDPERP explicitly says that on weekends "prices come from the perpetual market alone."
  
  So on weekends you can, for the first time, hold *both* legs (MT5 broker perp versus xyz:GOLD), each priced by a different thin market under different bounds and funding rules. That is a real **stat-arb/spread mean-reversion** candidate. Its edge size is unknown until you log data, and it carries model risk because the broker's reference may itself be Hyperliquid or another crypto venue. Holding periods would be hours to the end of the weekend, with capital of about $10–30k across both legs.

### D. Oracle-lag / mark-price arbitrage on HIP-3 — **Rare but large; best done as "dislocation capture"**

- **How it works.** HIP-3 marks are bounded and median-filtered, but the book can still gap in thin moments:
  - On 4 July 2026, xyz:GOLD fell about **$100 (~240 bps) in under a minute**, below $4,090, before arbitrage bots restored it. That echoed a May SpaceX pre-IPO crash that liquidated $1.51M.
  - In the first Hormuz shock, CL depth within ±10 bps collapsed from $65K to about $256, and median spreads widened to 13.9–24 bps.
- **The broker-safe pattern.** Rest deep limit orders on Hyperliquid, for example 0.5–1.5% away from the oracle, sized to the book. When one fills, immediately hedge on MT5 XAUUSD at the broker's *fair* price, then unwind both legs as Hyperliquid converges, over minutes to hours.
  - This is not latency arbitrage against the broker. You aren't exploiting a stale MT5 quote, and the MT5 trade looks like a normal hedged position. That is far less likely to trigger "toxic flow" action.
- **Costs and edge.** Hyperliquid maker fees on gold are 3 bps per side, and MT5 costs about 0.5 bps. Against a 100–250 bps dislocation the net edge is large, but frequency is low: a handful of events a year per market, clustered on thin hours and holidays.
- **The risk is not getting filled sensibly.** A fill may be the start of a real move, not a glitch. The MT5 hedge must go on within seconds, and MT5 may itself be closed (weekends) or slow. Expect a few fills a year at small size. It is worth automating because the bot is idle most of the time.

### E. Statistical spread mean-reversion (weekday) — **Possible on growth-mode markets, not on gold**

Model `spread = HL_mid − MT5_mid − fair_basis(t)`. On SP500, XYZ100, SILVER and CL, the Hyperliquid round trip costs about 1–2 bps, so z-score reversion trades on the residual can clear costs if your MT5 broker's index or silver CFD spread is tight. Hold for minutes to hours, and don't fire on sub-second signals so the broker doesn't flag you. Compare silver (growth mode, 0.9 bps taker) with gold (9 bps): as a gold trader, you may find **XAGUSD vs xyz:SILVER** the cheaper metals pair to test first.

## Cost, Capital and Margin Model (gold, 0.1-lot hedge ≈ 10 oz ≈ $43.6k notional)

| Item | MT5 leg (ECN) | Hyperliquid leg (xyz:GOLD) |
|---|---|---|
| Entry+exit trading cost | ~$2 (spread ~$0.03–0.12/oz + $0.70 commission) | taker ~$78 (18 bps) / maker ~$26 (6 bps) |
| Carry | long −$5.4 to −7.2/night; short +$1.9 to +3.7/night | ~5.5%/yr ≈ $6.6/day paid by longs to shorts |
| Margin | ~$436 at 1:100; ~$2.2k at 1:20 (ASIC/CySEC retail cap) | ~$1.75k at 25x; prudent 3–5x = $8.7–14.5k |
| Transfers | broker deposit rails (crypto/mobile money vary) | Arbitrum USDC in (≥5 USDC; smaller deposits are lost), $1 withdrawal |

Capital split is the hidden cost. To survive a 5–10% adverse move on one leg without liquidation while the other leg profits, you need excess margin on *both* venues and a rebalancing routine. Moving USDC between Hyperliquid and the broker takes minutes on-chain but often hours to days on the broker side. Plan for about $10–25k total for a serious live test of any two-leg strategy at 0.1-lot scale.

## Execution & Infrastructure

**Hyperliquid:**
- Use the official Python SDK, or raw REST/WebSocket (`api.hyperliquid.xyz`, `wss://api.hyperliquid.xyz/ws`), with `dex: "xyz"` for HIP-3 markets.
- Trade through API (agent) wallets so your main key stays cold.
- Rate limits per IP: 1,200 REST weight/min; 10 WebSocket connections; 1,000 subscriptions; 2,000 outbound messages/min; 100 in-flight posts.
- There is also an address-based budget of 1 request per 1 USDC traded cumulatively. When you're rate-limited it drops to 1 request per 10 seconds, so a low-volume quoting bot can lock itself out.
- Hyperliquid recommends Tokyo for lowest latency.
- A self-run node needs at least 16 vCPU, 128 GB RAM and 500 GB fast SSD, roughly $725–1,067/month. A private gateway such as HypeRPC costs $1,999/month for 10x limits.
- Median end-to-end settlement is about 200 ms.

**MT5:**
- The official `MetaTrader5` Python package is Windows-only. It drives a running terminal on the same machine, and `order_send()` is synchronous, with no async equivalent.
- For lowest latency, run an MQL5 EA that streams ticks and receives orders over a socket or ZeroMQ, using `OrderSendAsync`.
- Realistic full-fill times are about 60–200 ms on MT5, and minutes during news on bad servers.
- MT5 Manager API access is for brokers, not clients.

**Geography problem.** Hyperliquid's sweet spot is Tokyo. Most MT5 broker servers sit in Equinix LD4 (London) or NY4 (New York), about 150–250 ms away. No single VPS is close to both, and Kigali is far from all three. This alone rules out sub-second arbitrage and favours strategies B–E.

**Reference architecture (fits your stack):**
1. **Tokyo VPS (AWS ap-northeast-1).** A Python/FastAPI or Rust Hyperliquid connector, with WebSocket L2/trades/oracle for xyz:GOLD, SP500 and SILVER, plus order and agent-wallet signing.
2. **Windows VPS in the broker's data centre (LD4/NY4).** An MT5 terminal with a bridge EA that publishes ticks and accepts orders over ZeroMQ.
3. **Central engine (NestJS or Python) colocated with whichever leg is the *hedge*.** It reads both feeds via Redis Streams, computes the fair basis and z-scores, and enforces risk:
   - maximum naked-leg time (for example, 3 seconds, then flatten);
   - per-venue margin floors;
   - a weekend mode;
   - a kill switch on stale feeds (for example, more than 2 seconds without a Hyperliquid oracle update).
4. **PostgreSQL/Timescale.** Tick storage and fills, for reconciliation and research.
5. **Alerting.** Funding regime changes, discovery-bound proximity, ADL events (fill `dir` equals "Auto-Deleveraging"), and broker requotes and rejects.

## Risks and Barriers

**Broker side (decisive for latency strategies):**
- Most market-maker and B-book brokers prohibit arbitrage and latency trading. They detect toxic flow (short hold times, consistent stale-quote wins) and can widen spreads, switch you to manual or last-look execution, add requotes, or void profits.
- IC Markets' clause 12.3 lets it void transactions affected by price latency, revoke profits and block trading "without prior notice."
- The UK Financial Ombudsman has sided with brokers. In decision DRN2518059 (Vantage FX), about €2,000 of latency-arbitrage profit was removed; DRN-4314274 (TigerWit) also went the broker's way.
- Some sites claim Tickmill and RoboForex allow arbitrage, but those claims come from vendors selling arbitrage tools and are unverified. Get written confirmation.
- Prop firms (FTMO, FundedNext, The5ers) ban it outright.

**Hyperliquid side:**
- **Manipulation of thin markets:**
  - JELLY (March 2025): HLP took "roughly $12 million in unrealized losses" (The Block), ended when the validator set voted to delist JELLY perps.
  - XPL pre-market (27 August 2025): four wallets made a combined $46M as XPL spiked 2.5x to $1.80 on Hyperliquid alone, with $17M of liquidations; Hyperliquid says it incurred no bad debt.
  - POPCAT (12 November 2025): an attacker spread $3M USDC across 19 wallets to build a $20–30M long, leaving HLP with $4.9M of bad debt (CoinDesk, citing Lookonchain); deposits and withdrawals were briefly paused.
  - A Fartcoin incident (April 2026).
- **Auto-deleveraging:** on 10 October 2025 there were 34,983 ADL executions across 19,337 wallets and 162 markets. Delta-neutral traders had one leg force-closed. ADL is your biggest two-leg risk: your winning Hyperliquid leg can be closed while your MT5 leg stays open.
- **Weekend liquidation:** you can be liquidated on xyz on weekends while your MT5 hedge is frozen.
- **HIP-3 deployer risk:** the deployer controls oracles, leverage, bounds and delistings. Deployers have shut down (Felix) or closed markets (Ventuals). Funding, bounds and fees "may change," per trade.xyz's own disclaimers.
- **Flash dislocations:** gold on 4 July 2026, and the SpaceX crash on Ventuals.

**Counterparty risk.** An offshore MT5 entity (for example, Seychelles, which offers higher leverage) offers weaker protection than ASIC or CySEC entities, which cap retail gold at 1:20. On Hyperliquid you're exposed to the bridge, the validators and HLP. Funds are non-custodial but subject to protocol pauses.

**Rwanda specifics:**
- Rwanda's Capital Market Authority issued its Regulations Governing Leveraged Foreign Exchange Trading in Rwanda on 26 February 2024 (published in the Official Gazette on 27 February, per The New Times), with a maximum leverage ratio of 100:1. They require firms offering leveraged FX to the public in Rwanda to be authorized. Individual traders don't need a licence, and most Rwandans use offshore-regulated international brokers.
- Hyperliquid's Terms (updated 15 June 2026) restrict the US, Ontario and sanctioned territories. Rwanda is not restricted, and there is no KYC.
- The Terms also state Hyperliquid holds no licence anywhere and that you're responsible for local-law compliance.
- Practical frictions: banking USDC on- and off-ramps, documenting source of funds for broker KYC, and tax treatment. Take local advice on the tax position.

## Ranked Opportunities

| Rank | Opportunity | Est. edge (net) | Frequency / hold | Capital | Risk | Complexity | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | **Weekend pair: MT5 broker perp CFD (Pepperstone GOLD/NAS100/SP500-PERP) vs xyz perps** | Unknown; plausibly 10–50 bps per episode on thin weekends (must measure) | Most weekends; hours | $10–30k | Medium-high (synthetic reference, bounds, broker terms) | Medium | **Realistic — test first** |
| 2 | **Dislocation capture: resting Hyperliquid limits + MT5 hedge** | 100–250 bps per event, minus ~4 bps costs | A few a year per market; minutes to hours | $10–25k | Medium (fill-into-real-move, hedge latency) | Medium | **Realistic, low frequency** |
| 3 | **Weekend signal for Monday gold/index open (your scalping filter)** | Directional; 94.9% direction hit rate on moves over 100 bps | Weekly | Existing account | Directional risk | Low | **Realistic as signal, not arbitrage** |
| 4 | **Weekday stat-arb on growth-mode pairs (SILVER, SP500, XYZ100, CL)** | 1–5 bps per trade if the MT5 spread is tight | Daily; minutes to hours | $10–20k | Medium (basis model, broker flags) | High | **Marginal — research only** |
| 5 | **Conditional crypto carry (negative Hyperliquid funding + broker short credit)** | 5–20%/yr annualized while the regime lasts | Episodic; days to weeks | $5–20k | ADL, regime flips | Low | **Opportunistic** |
| 6 | Gold funding/swap carry | ~0 (−0.5 to +0.7%/yr) | — | — | — | Low | **Illusory** |
| 7 | MT5 vs Hyperliquid latency arbitrage on gold | Negative after 19 bps costs; broker-voidable | — | — | Very high | High | **Illusory — don't build** |

## Recommended Broker Characteristics

- The client agreement does *not* list "arbitrage," "latency," "lag trading" or "stale-price exploitation" as abusive. Get written confirmation from support, and save it.
- True ECN/STP or agency execution with raw spreads and commission. Avoid "dealing desk," "virtual dealer" behaviour and last-look rejects. Test fill ratios on demo *and* small live.
- 24/7 perpetual CFDs on MT5 for gold and indices, with the weekend reference methodology disclosed.
- Server location documented (LD4/NY4) and a VPS available in the same data centre.
- A positive short swap on crypto, and swap rates that are published and stable.
- A reputable regulator for the entity you actually sign with, and crypto or mobile-money funding rails that work from Rwanda.

## Step-by-Step Validation Plan

1. **Weeks 1–2: data collection.**
   - Log tick-level MT5 XAUUSD, XAGUSD, US500, NAS100 and any -PERP symbols from 2–3 candidate brokers, using the EA → ZeroMQ → Timescale pipeline.
   - In parallel, log Hyperliquid xyz:GOLD, SILVER, SP500, XYZ100 and CL (L2, trades, oracle, mark, funding) from a Tokyo VPS.
   - Buy or download history from 0xArchive's HIP-3 API to backfill weekends and dislocations.
2. **Weeks 2–4: measurement.**
   - Weekday basis distributions (p50/p95/p99) per pair after timestamp alignment.
   - Weekend broker-perp vs xyz spread paths.
   - Count of dislocations above 50, 100 and 200 bps.
   - Funding against swap over time.
3. **Backtest with pessimistic costs.**
   - Hyperliquid taker fees on gold (9 bps); MT5 slippage of at least 1 tick; weekend depth at 30% of weekday levels.
   - Include triple-swap days (Wednesday for metals; Friday for crypto and indices at many brokers).
   - Reject any strategy whose median edge is less than twice total costs.
4. **Weeks 5–8: small live test.** Run 0.01–0.05 lots on MT5 against equivalent Hyperliquid size, with a naked-leg timeout and daily reconciliation. Track broker behaviour: rejects, requotes, slippage asymmetry and any spread widening on your account.
5. **Scale decision.** Scale only strategies 1–2 (and 5 when its funding regime appears) that show positive live P&L net of fees over at least 8 weekends or at least 5 dislocation fills, with no broker intervention. Re-verify Hyperliquid fees and growth-mode status monthly.

## Caveats

- Several figures are point-in-time snapshots:
  - ARX gold OI and funding;
  - broker swaps (Afterprime dated 15 October 2026; IC Markets via Myfxbook, undated);
  - Pepperstone's crypto financing example from a cost-disclosure PDF.
  Re-pull them before use.
- The weekend price-discovery research was funded by the Hyperliquid Policy Center and produced by firms selling Hyperliquid data. Earlier, smaller samples showed much weaker results.
- Pepperstone's perp reference venue is undisclosed. If it is effectively Hyperliquid, the "pair" in Rank 1 may be tighter, and less profitable, than hoped.
- Fee schedules for HIP-3 are in flux, with AMBCrypto (6 Aug 2026) reporting that deployers may raise fees on specific assets by up to 3x, versus reports of growth mode being kept. Gold's non-growth status is the single biggest swing factor for gold strategies.
- Nothing here is legal or tax advice. Broker policies vary by entity and change without notice.