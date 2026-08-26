// One unified sidebar. Dropping `type: 'page'` moves each product out of the
// navbar and into the sidebar as a collapsible group, so the whole corpus is
// navigable from any page.
//
// Starred defaults match docs/lib/projects.ts DEFAULT_STARRED (landing page
// stars are user-editable in the browser; this group is the curated seed).
export default {
  index: 'Introduction',
  collections: 'Postman Collections',
  '---starred': {
    type: 'separator',
    title: 'Starred',
  },
  'pump-fun': 'Pump.fun Scalper',
  'lookup-trader': 'Lookup Trader',
  'lux-algo': 'LuxAlgo',
  'ipda': 'IPDA',
  'signals-scrapper': 'Signals Scrapper',
  '---strategies': {
    type: 'separator',
    title: 'Strategies',
  },
  'vrvp-strategy': 'VRVP Strategy',
  'jesse-strategies': 'Jesse Strategies',
  'tinga-tinga': 'Tinga Tinga',
  'binance-crypto': 'Binance Crypto',
  'fu-strategy': 'FU Strategy',
  'bitcoin9to5': 'Bitcoin 9to5',
  '---execution': {
    type: 'separator',
    title: 'Execution',
  },
  'mt5-trader': 'MT5 Trader',
  'forex-execution': 'Forex Execution',
  'telegram-metatrader': 'Telegram → MT5',
  '---data': {
    type: 'separator',
    title: 'Data',
  },
  'ctrader-markets': 'cTrader Markets',
  'telegram-bot': 'Telegram Bot',
  '---infrastructure': {
    type: 'separator',
    title: 'Infrastructure',
  },
  'notification-service': 'Notification Service',
};
