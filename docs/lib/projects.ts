export type Project = {
  id: string;
  title: string;
  href: string;
  description: string;
};

/** Seed stars for first visit (and sidebar featured group). */
export const DEFAULT_STARRED: readonly string[] = [
  'pump-fun',
  'lookup-trader',
  'lux-algo',
  'ipda',
  'signals-scrapper',
];

export const PROJECTS: readonly Project[] = [
  {
    id: 'pump-fun',
    title: 'Pump.fun Scalper',
    href: '/pump-fun',
    description:
      'Automated Solana bot that detects pump.fun graduations, screens them through a 10-check guardrail engine, and scalps small positions with sub-second exits.',
  },
  {
    id: 'lookup-trader',
    title: 'Lookup Trader',
    href: '/lookup-trader',
    description:
      'Local bar-replay and manual labelling tool for building a pattern-based probability and outcome database.',
  },
  {
    id: 'lux-algo',
    title: 'LuxAlgo',
    href: '/lux-algo',
    description:
      'Supertrend signal service that polls candles, applies confluence overlays, and submits orders to MT5 Trader.',
  },
  {
    id: 'ipda',
    title: 'IPDA',
    href: '/ipda',
    description:
      'RSI Buy Chance / Sell Chance signal service with session gates, notification-service alerts, and break-even advisory — submits to MT5 Trader.',
  },
  {
    id: 'signals-scrapper',
    title: 'Signals Scrapper',
    href: '/signals-scrapper',
    description:
      'Scheduled NestJS bot that screenshots IC Markets research pages, extracts signals with OpenAI vision, and optionally forwards them to MT5 Trader.',
  },
  {
    id: 'mt5-trader',
    title: 'MT5 Trader',
    href: '/mt5-trader',
    description:
      'Authenticated FastAPI service that validates and idempotently executes trading signals through a local MetaTrader 5 terminal.',
  },
  {
    id: 'fu-strategy',
    title: 'FU Strategy',
    href: '/fu-strategy',
    description:
      'Capital.com FU / multi-timeframe strategy with HTF bias and zones, WhatsApp/SMS alerts, and optional 1M auto-execution.',
  },
  {
    id: 'bitcoin9to5',
    title: 'Bitcoin 9to5',
    href: '/bitcoin9to5',
    description:
      'BTC perpetual bot on Nado that shorts US cash hours and longs overnight with adaptive zone timing.',
  },
  {
    id: 'ctrader-markets',
    title: 'cTrader Markets',
    href: '/ctrader-markets',
    description:
      'Profile-scoped FastAPI wrapper for the cTrader Open API — ticks, OHLC, and symbols over HTTP/SSE.',
  },
  {
    id: 'telegram-bot',
    title: 'Telegram Bot',
    href: '/telegram-bot',
    description:
      'GramJS user-session poller that detects Gold/XAU buy/sell phrases and fans out SMS via Pindo.',
  },
  {
    id: 'telegram-metatrader',
    title: 'Telegram → MT5',
    href: '/telegram-metatrader',
    description:
      'Windows Telethon copier that reads one Telegram chat and places fixed-lot MT5 market orders (dry-run by default).',
  },
  {
    id: 'forex-execution',
    title: 'Forex Execution',
    href: '/forex-execution',
    description:
      'TypeScript/Fastify OANDA REST-v20 service for authenticated account and instrument APIs (Phases 1–2).',
  },
  {
    id: 'notification-service',
    title: 'Notification Service',
    href: '/notification-service',
    description:
      'NestJS multi-channel notification API (Telegram, email, SMS, WhatsApp) with SQLite delivery history.',
  },
  {
    id: 'vrvp-strategy',
    title: 'VRVP Strategy',
    href: '/vrvp-strategy',
    description:
      'Multi-timeframe Forex trading system combining Supertrend, StochRSI, Fair Value Gap, and Volume Profile indicators.',
  },
  {
    id: 'jesse-strategies',
    title: 'Jesse Strategies',
    href: '/jesse-strategies',
    description:
      'Auction Market Theory based strategies using the Jesse AI framework including trend continuation and mean reversion.',
  },
  {
    id: 'tinga-tinga',
    title: 'Tinga Tinga',
    href: '/tinga-tinga',
    description:
      'RSI crossover-based Forex/Crypto strategy with Binance integration, risk management, and backtesting capabilities.',
  },
  {
    id: 'binance-crypto',
    title: 'Binance Crypto',
    href: '/binance-crypto',
    description:
      'TypeScript/JavaScript strategies for Binance cryptocurrency exchange with utility modules for indicators.',
  },
];

export const STARRED_STORAGE_KEY = 'ta-docs-starred';

export function parseStarred(raw: string | null): string[] | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    const ids = new Set(PROJECTS.map((p) => p.id));
    return parsed.filter((id): id is string => typeof id === 'string' && ids.has(id));
  } catch {
    return null;
  }
}
