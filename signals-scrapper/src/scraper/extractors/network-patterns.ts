/**
 * URL patterns for Trading Central / Recognia research API responses.
 * IC Markets embeds TC in iframe#tradingcentral → site.recognia.com/...serve.shtml?tkn=...
 */
export const TRADING_CENTRAL_NETWORK_PATTERNS: RegExp[] = [
  /recognia/i,
  /site\.recognia\.com/i,
  /serve\.shtml/i,
  /trading.?central/i,
  /tctrading/i,
  /research.*idea/i,
  /\/ideas\b/i,
  /opinion/i,
  /technical-analysis/i,
  /productanalysis/i,
  /trade.?idea/i,
  /story/i,
  /chart\/json/i,
];

/** URL patterns for Autochartist research API responses. */
export const AUTOCHARTIST_NETWORK_PATTERNS: RegExp[] = [
  /autochartist/i,
  /auto.?chartist/i,
  /pattern/i,
  /trade.?idea/i,
  /\/signals?\b/i,
  /research/i,
];
