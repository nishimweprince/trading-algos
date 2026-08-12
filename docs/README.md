# Trading Algos Documentation

Documentation site for Trading Algos, built with [Nextra 4](https://nextra.site/) (docs theme), Next.js App Router, and MDX.

## Getting started

Prerequisites: Node.js 18+ and npm.

```bash
npm install
npm run dev
```

The site runs at `http://localhost:3000`.

```bash
npm run build   # production build; postbuild indexes the site for search
npm start       # serve the production build
```

Search is [Pagefind](https://pagefind.app/), which indexes the built HTML. It only works against a production build — the `postbuild` script generates the index into `public/_pagefind`, so `npm run dev` has no search results.

## Project structure

Content lives inside `app/` as co-located `page.mdx` files — the Nextra 4 App Router convention. There is no separate `content/` directory.

```
docs/
├── app/
│   ├── layout.tsx            # Root layout: fonts, theming, Nextra <Layout>
│   ├── global.css            # All custom styling (see "Theming")
│   ├── _meta.js              # Top-level sidebar order and grouping
│   ├── page.mdx              # Introduction
│   └── <section>/
│       ├── _meta.js          # Section sidebar order
│       ├── page.mdx          # Section overview
│       └── <page>/page.mdx   # Individual pages
├── lib/
│   └── shiki-monochrome.mjs  # Monochrome syntax-highlighting themes
├── mdx-components.tsx        # Registers Callout, Cards, Steps, Tabs globally
├── next.config.mjs
└── package.json
```

## Sections

| Section | Path | Description |
|---|---|---|
| VRVP Strategy | `/vrvp-strategy` | Multi-timeframe Forex system (Supertrend, StochRSI, FVG, Volume Profile) |
| Jesse Strategies | `/jesse-strategies` | Auction Market Theory strategies on the Jesse framework |
| Tinga Tinga | `/tinga-tinga` | RSI crossover strategy with Binance integration |
| Binance Crypto | `/binance-crypto` | TypeScript strategies and indicator utilities |
| FU Strategy | `/fu-strategy` | Capital.com FU / MTF strategy with notifications and 1M auto-exec |
| LuxAlgo | `/lux-algo` | Supertrend signal service that posts to MT5 Trader |
| IPDA | `/ipda` | IPDA Supertrend×SMA service with sessions, notifications, and break-even advisory |
| Bitcoin 9to5 | `/bitcoin9to5` | BTC perp bot on Nado (short US hours, long overnight) |
| MT5 Trader | `/mt5-trader` | FastAPI service executing signals through MetaTrader 5 |
| Pump.fun Scalper | `/pump-fun` | Solana bot scalping pump.fun graduations |
| Forex Execution | `/forex-execution` | OANDA REST-v20 account/instrument service (Phases 1–2) |
| Telegram → MT5 | `/telegram-metatrader` | Telegram chat → fixed-lot MT5 copier (Windows) |
| Signals Scrapper | `/signals-scrapper` | NestJS bot extracting signals from research pages |
| cTrader Markets | `/ctrader-markets` | Profile-scoped cTrader Open API HTTP/SSE wrapper |
| Telegram Bot | `/telegram-bot` | GramJS channel poller → Pindo SMS |
| Lookup Trader | `/lookup-trader` | Bar replay, labelling, pattern DB, outcome/meta models |
| Notification Service | `/notification-service` | Multi-channel NestJS notification API |

Sidebar grouping (Strategies / Execution / Data / Research / Infrastructure) is defined in `app/_meta.js`.

## Adding a page

1. Create `app/<section>/<page>/page.mdx` with frontmatter:

   ```mdx
   ---
   title: Page Title
   description: Brief description
   ---

   # Page Title
   ```

2. Add the directory name as a key in that section's `_meta.js` to place it in the sidebar. Pages missing from `_meta.js` are appended alphabetically.

Use `type: 'separator'` entries to group pages under a heading — see `app/pump-fun/_meta.js` for the pattern.

### Components

`Callout`, `Cards`, `Steps`, and `Tabs` are registered globally in `mdx-components.tsx`, so MDX files can use them without importing.

Valid `Callout` types are `default`, `info`, `warning`, `error`, and `important`. Any other value renders with no icon and no styling.

## Theming

The site is strictly monochrome — black, white, and gray only. No accent hue. State (focus rings, active TOC, link hover, warning icons) uses the same gray ramp as the rest of the chrome. Text selection is a monochrome inversion.

Three places control the look:

- **`app/global.css`** — all custom styling. Nextra ships prebuilt Tailwind v4 as `@layer theme, base, components, utilities`; every rule in `global.css` is deliberately unlayered so it beats all four layers. This only works because `layout.tsx` imports it *after* `nextra-theme-docs/style.css`, so keep that order.
- **`app/layout.tsx`** — the `<Head color={...} backgroundColor={...} />` props feed Nextra's `--nextra-primary-*` and `--nextra-bg` variables, which the theme uses to derive its accent and background.
- **`lib/shiki-monochrome.mjs`** — the light and dark syntax-highlighting themes, wired up in `next.config.mjs`.

This project does **not** use Tailwind directly. Styling is plain CSS against the token set defined at the top of `global.css`.

## Deployment

```bash
npm run build
vercel deploy
```

Any host that runs `npm run build && npm start` works. Make sure the build step runs `postbuild` (npm does this automatically) or search will be missing.

## License

MIT
