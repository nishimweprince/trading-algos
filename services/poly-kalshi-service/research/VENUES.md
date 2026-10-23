# Prediction-market venue survey for `poly-kalshi-service`

Research date: **2026-10-23**

## Bottom line

The strongest new independent books for the existing Kalshi-led, 15-minute crypto
strategy are:

1. **Gemini Predictions** — best overall API and contract-shape match; BTC, ETH, SOL,
   and XRP.
2. **OKX Event Contracts** — native 15-minute BTC, ETH, and SOL contracts with public
   REST/WebSocket market data and trading endpoints. **Built first, as `pk-okx`
   (paper, Tokyo, 2026-10-23); see the README's "pk-okx" section.**
3. **Nadex / Crypto.com Derivatives North America (CDNA)** — the broadest short-duration
   US-regulated crypto catalog, but FIX/onboarding and jurisdiction make integration
   materially harder.
4. **Predict.fun** — a real CLOB on BNB Chain with verified BTC and ETH 15-minute
   markets and a documented developer API.
5. **Polymarket US** — an independent US-regulated book, distinct from the Polygon
   Polymarket CLOB already used by this service.
6. **Jupiter Forecast, native `bisonfi` provider** — BTC 15-minute markets on Solana;
   useful because the project already has Solana/Helius infrastructure.

Start all six as **record-only** feeds. None should be called a risk-free arbitrage leg
until its strike, oracle, sampling interval, tie rule, correction policy, fees, and
account eligibility have been captured per contract.

## What qualifies as another venue

The current strategy needs more than a page displaying “up/down.” A useful leg has:

1. a recurring window aligned with Kalshi’s clock;
2. an independent price or liquidity pool rather than a broker routing to Kalshi or
   Polymarket;
3. observable bids/offers or executable quotes;
4. documented settlement rules and price-source provenance; and
5. an API suitable for continuous discovery and market-data recording.

Contracts on the same asset and clock are still not necessarily equivalent. The
settlement index is part of the instrument.

## Tier A — build recorders next

| Rank | Venue | Current short-duration match | Market / API model | Settlement difference | Recommendation |
|---:|---|---|---|---|---|
| 1 | **Gemini Predictions** | Native 5m/15m BTC, ETH, SOL, XRP; live 15m listings verified | Public event discovery; public book-ticker, depth, trades and index WebSockets; REST/WS trading | Gemini reference index; `close >= strike` resolves YES | Build first. Strongest combination of overlap, CLOB microstructure and API quality. Execution is jurisdiction-dependent. |
| 2 | **OKX Event Contracts** | Native 5m/15m BTC, ETH, SOL; live `EVENTS` API | Public REST and WebSocket market data; private order API; YES/NO CLOB; non-post-only orders require `speedBump=1` | OKX spot index; 15m settlement is a 60-second average of one-second candle closes | Build second. Excellent shape and API. Verify account-region eligibility and real depth before paper execution. |
| 3 | **Nadex / CDNA** | Recurring $1 5m/15m crypto event contracts; current specs include BTC and ETH and a wider crypto catalog | Exchange CLOB; $0.02 per-contract entry/exit; FIX available while REST/WS prediction trading is still listed as coming soon | Nadex asset index and its defined settlement calculation | High-value observer. US-only FCM access and institutional connectivity are the main blockers. Count Nadex/CDNA/Crypto.com Predict Pro once. |
| 4 | **Predict.fun** | Verified BTC and ETH 5m/15m; other current assets must be discovered dynamically | BNB Chain conditional tokens with off-chain CLOB; beta REST/SDK; API key | Chainlink DataLink sourced from Binance; documented BTC 15m equality pays 50/50 | Build a direct adapter. Binance Wallet Prediction is only another frontend for this book. |
| 5 | **Polymarket US** | Live 15m crypto listings including BTC, with ETH/SOL/XRP expansion observed | Independent US-regulated exchange, not the global Polygon CLOB | Contract-specific and must be captured from the listing | Pursue programmatic market-data onboarding. Keep venue IDs and positions separate from global Polymarket. |
| 6 | **Jupiter Forecast / BisonFi** | BTC 15m | Solana outcome tokens and atomic USDC swaps via Jupiter Prediction API; API key; REST discovery | Chainlink BTC/USD | Cheap record/paper target because Helius already exists. Only `provider=bisonfi` is independent; Jupiter also aggregates Kalshi and Polymarket. |

## Tier B — measure, but do not put in the existing active-exit engine

| Venue | Why it is interesting | Why it is not a drop-in leg |
|---|---|---|
| **Forsee** | BTC, ETH, SOL, BNB, XRP and HYPE up/down contracts over 1m/5m/15m; also FX, gold and silver; documented REST API | A fixed-quote market maker, not a CLOB; about 2% payout fee, positions lock, final-order cutoff, REST polling and limited API rollout. Suitable for hold-to-settlement experiments only. |
| **PancakeSwap Prediction** | Rolling 5m BNB/BTC/ETH on BNB Chain; separate fixed-expiry 15m “Dumb Mode” for BTC/ETH/SOL/BNB | Parimutuel or house-style payout, locked positions and no convergence exit. It produces a directional trade, not the current book-lag trade. |
| **Hyperliquid HIP-4** | Public outcome-market catalog and normal Hyperliquid book APIs; BTC/ETH/SOL daily and short threshold events were live | No recurring start-vs-end 15m family was verified. Many short contracts are price-threshold-at-time instruments settled by Pyth or Hyperliquid mark. Watch `outcomeMeta`. |
| **Deriv Rise/Fall** | Quote, buy, monitor and in some cases sell APIs across FX and other instruments | Broker/dealer binary, not a crowd CLOB; tick and entry-relative rules do not map cleanly to fixed UTC windows. |
| **CME event contracts / broker distribution** | Regulated crypto and macro events and potential institutional routing | Different underlyings/expiries and broker entitlements; treat as a separate strategy until exact listings and access are verified. |

## Tier C — broader prediction-market inventory

These venues are useful for a future general-event adapter, but no recurring contract
equivalent to the current crypto 15-minute strategy was verified.

| Venue | Model / API | Current fit |
|---|---|---|
| **Opinion** | Public REST market/order-book data, SDK and WebSocket trading | Strong general CLOB infrastructure; no verified recurring 15m crypto family. |
| **Pascal** | Solana non-custodial CLOB; public REST/WS and trading APIs | Excellent technical adapter target for general events; no direct short-crypto match found. |
| **Myriad** | On-chain event markets on Abstract/BNB-style ecosystems; public API | General-event expansion; automated short markets need separate validation. |
| **Futuur** | Real-money prediction market with market-maker API and Pusher stream | General-event candidate; match contracts individually. |
| **Rain** | Arbitrum prediction markets; unauthenticated read API and SDK path | Good catalog/reference feed; no verified 15m crypto family. |
| **XO Market** | User-created CLOB; off-chain matching/on-chain settlement; API | Oracle and market-creator risk require explicit gating. |
| **Bayse Markets** | Africa-focused CLOB + AMM; REST/WS order and settlement APIs | Regionally relevant, but no verified recurring 15m crypto series. |
| **SX Bet** | Sports CLOB API | Separate sports strategy only. |
| **ProphetX, Novig, Sporttrade** | US sports exchanges with gated/partner APIs | Separate sports strategy only. |
| **PredictIt** | US political contracts and public data | Politics only and structurally constrained. |
| **ForecastEx / IBKR ForecastTrader** | CFTC event contracts distributed through IBKR | Macro/economic expansion, not a native crypto-window leg. |
| **Manifold, Metaculus, Good Judgment** | Play-money or forecasting platforms | Signal/research only; no executable real-money leg. |

## Frontends and aggregators to deduplicate

Do not inflate the venue count with another UI over the same liquidity:

- **Coinbase prediction markets** route to **Kalshi**.
- **Binance Wallet Prediction** uses **Predict.fun** as its upstream order book.
- **Crypto.com Predict Pro**, **CDNA**, **OG Predictions**, and the current **Nadex**
  event contracts belong to the same exchange family.
- **Jupiter** aggregates Kalshi and Polymarket listings; only the native
  `provider=bisonfi` markets represent an additional venue.
- Robinhood and Webull Kalshi offerings should likewise be treated as Kalshi
  distribution until they list a demonstrably independent book.

Discovery/normalization services such as **Prediction.com**, **Synpath**,
**Predictefy**, **pdata**, **Prediction Market Radar**, and **Resolved Markets** can
accelerate catalog mapping and historical backfill. They are not execution venues.
Use direct venue connections for the hot path.

## Settlement equivalence grades

Store a grade with every cross-venue match:

- **E0 — equivalent:** same outcome, source, time window, tie rule and correction policy.
- **E1 — correlated:** same window and direction but different oracle, strike sample,
  averaging interval, tie rule or correction policy.
- **E2 — related:** same underlying with a different timeframe, threshold or contract
  shape.
- **E3 — reference:** spot, perp, options, broker quotes, or play-money forecasts.

The new short-duration venues currently start as **E1**, not E0. That means the
existing Kalshi-to-venue trade remains a signal trade; it is not guaranteed cross-venue
arbitrage.

## FX and traditional-exchange path

Kalshi now has 15-minute EUR/USD, GBP/USD and USD/JPY contracts. This deserves a
separate experiment:

- **Forsee** exposes short-duration FX up/down quotes, but the positions lock.
- **Nadex/CDNA** has FX event contracts, although contract strikes and durations must
  be matched rather than assumed equivalent.
- **OANDA v20**, **Saxo OpenAPI**, **LMAX/FIX**, and **Interactive Brokers** can supply
  executable FX prices. They are reference feeds, not prediction markets.

The first FX study should record Kalshi plus OANDA and one institutional-grade stream,
identify Kalshi’s exact observation source, and measure lead/lag. Here the likely
direction is the reverse of the crypto strategy: spot FX may lead the Kalshi book.

For crypto reference data, the service already records Binance spot/perp, Kraken,
Bitstamp, Coinbase, Helius and PolyBolt. The next high-value feeds are the actual
settlement inputs—Chainlink, Pyth, Gemini’s named index, OKX’s index, and the Nadex
index—not merely more generic spot exchanges.

## Recommended implementation shape

### 1. Add a venue-contract descriptor

Do not force all venues into the current Kalshi/Polymarket-shaped `Window`. Persist:

- venue and venue market IDs;
- asset, open time, close time and duration;
- outcome mapping;
- strike/open source and close source;
- oracle sampling/averaging window;
- equality/tie rule and correction/dispute policy;
- market model: CLOB, AMM, fixed quote, or parimutuel;
- fee model, minimum size and taker delay;
- whether the position can exit before settlement; and
- equivalence grade plus the rule/specification hash used for the match.

### 2. Isolate executable venues

Follow the existing `pk-limitless` pattern. Each executable venue should have its own
discovery, sockets, state, tape, risk gate and process failure domain. Reference-only
feeds can remain observers. Replace one-character feed codes with a versioned venue
registry before the alphabet becomes an opaque protocol.

### 3. Promote slowly

1. Discover markets and record raw venue frames plus normalized BBO.
2. Run data-only for at least 72 hours.
3. Measure listing coverage, spread/depth, Kalshi lead/lag and settlement disagreement.
4. Paper trade with venue fees, real network latency, partial fills, speed bumps and
   locked-position rules.
5. Enable live trading only after per-venue position limits, stale-feed checks and kill
   switches pass.

## Primary sources

- Gemini Predictions: [ticker rules](https://developer.gemini.com/prediction-markets/tickers-crypto), [market discovery](https://developer.gemini.com/prediction-markets-spec/markets), [WebSocket streams](https://developer.gemini.com/trading/websocket/streams)
- OKX: [Event Contracts FAQ](https://www.okx.com/en-gb/help/event-contracts-faq), [settlement logic](https://www.okx.com/en-gb/help/how-to-trade-event-contracts-and-settlement-logic), [API v5](https://www.okx.com/docs-v5/)
- Nadex/CDNA: [crypto contract specifications](https://www.nadex.com/crypto/), [event-contract fees](https://website-prod.nadex.com/pricing/), [Crypto.com Predict Pro relationship and availability](https://help.crypto.com/en/articles/17213297-overview), [prediction API status](https://crypto.com/exchange-pro/en-GB/api)
- Predict.fun: [developer API](https://dev.predict.fun/), [BTC 15m rules](https://predict.fun/market/btc-updown-15m-1792671300), [Binance Wallet upstream disclosure](https://developers.binance.com/en/docs/products/w3w-prediction/websocket-streams/orderbook)
- Polymarket US: [live market site](https://polymarket.us/)
- Jupiter Forecast: [Prediction / Forecast API](https://developers.jup.ag/docs/prediction/forecast)
- Forsee: [API documentation](https://api.forsee.market/)
- PancakeSwap: [Prediction](https://docs.pancakeswap.finance/play/prediction), [Dumb Mode](https://docs.pancakeswap.finance/trade/perpetual-trading/perpetual-trading-v2/dumb-mode)
- Hyperliquid: [outcome asset IDs and API model](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids)
- Opinion: [Open API overview](https://docs.opinion.trade/developer-guide/opinion-open-api/overview)
- Pascal: [developer documentation](https://docs.pascal.trade/)
- Kalshi 15-minute financials: [category page](https://kalshi.com/category/financials/frequency/fifteen_min)
- FX reference APIs: [OANDA v20 pricing stream](https://developer.oanda.com/rest-live-v20/pricing-ep/), [Saxo pricing](https://www.developer.saxo/openapi/learn/pricing), [IBKR streaming market data](https://interactivebrokers.github.io/tws-api/market_data.html)

Availability, jurisdiction, fees, API access and contract rules can change. Revalidate
them immediately before production rollout. This survey is technical research, not a
claim that any particular account or hosting location is eligible to trade.
