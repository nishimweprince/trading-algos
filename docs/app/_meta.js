// One unified sidebar. Dropping `type: 'page'` moves each product out of the
// navbar and into the sidebar as a collapsible group, so the whole corpus is
// navigable from any page.
export default {
  index: 'Introduction',
  '---strategies': {
    type: 'separator',
    title: 'Strategies',
  },
  'vrvp-strategy': 'VRVP Strategy',
  'jesse-strategies': 'Jesse Strategies',
  'tinga-tinga': 'Tinga Tinga',
  'binance-crypto': 'Binance Crypto',
  '---execution': {
    type: 'separator',
    title: 'Execution',
  },
  'mt5-trader': 'MT5 Trader',
  'pump-fun': 'Pump.fun Scalper',
  '---data': {
    type: 'separator',
    title: 'Data',
  },
  'signals-scrapper': 'Signals Scrapper',
};
