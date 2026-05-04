/**
 * Example Telegram-style snippets for parser regression tests (anonymised).
 */
export const FIXTURE_MESSAGES = {
  goldBuyNow: 'Gold buy now',
  goldSellNow: 'Gold sell now',
  goldBuyCaps: 'GOLD BUY',
  emojiWrapped: '🚀 Gold BUY now 🚀',
  pastTense: 'We bought gold yesterday',
  ambiguous: 'Gold buy and sell levels',
  btc: 'BTC buy now',
  educational: 'Gold buy and sell are both valid strategies',
  xauUsd: 'XAU/USD sell now',
  xauToken: 'xau buy',
  withPunctuation: 'Gold sell now!!!',
  spaced: '  gold   sell   now  ',
};
