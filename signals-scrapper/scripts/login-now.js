#!/usr/bin/env node
/*
 * One-off IC Markets login through the scraper's Chrome (CDP), using the same
 * IcLoginService the scheduled runs use. Prints the outcome only; never prints
 * credentials. Run from the signals-scrapper directory after `npm run build`:
 *
 *   node scripts/login-now.js
 */
require('dotenv').config({ quiet: true });
const { chromium } = require('playwright');
const { AppConfigService } = require('../dist/config/app-config.service');
const { IcLoginService } = require('../dist/auth/ic-login.service');

(async () => {
  const config = new AppConfigService();
  const browser = await chromium.connectOverCDP(config.cdpEndpoint);
  const context = browser.contexts()[0];
  let page = context.pages().find((p) => /secure\.ic\.com/i.test(p.url()));
  if (!page) {
    page = await context.newPage();
  }
  const source = config.sources.find((s) => s.type === 'TRADING_CENTRAL') ?? config.sources[0];
  await page.goto(source.url, { waitUntil: 'networkidle', timeout: config.navTimeoutMs });
  console.log('before:', new URL(page.url()).pathname);
  const login = new IcLoginService(config);
  const outcome = await login.login(page);
  await page.waitForLoadState('networkidle', { timeout: config.navTimeoutMs }).catch(() => {});
  console.log('outcome:', outcome);
  console.log('after:', new URL(page.url()).pathname, '|', await page.title());
  await page.screenshot({ path: 'data/login-now.png' }).catch(() => {});
  await browser.close(); // detaches CDP only; the scraper's Chrome keeps running
})().catch((err) => {
  console.error('login-now failed:', err instanceof Error ? err.message : String(err));
  process.exit(1);
});
