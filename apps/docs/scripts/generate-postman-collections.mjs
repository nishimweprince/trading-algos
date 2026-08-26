import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const OUTPUT = join(ROOT, 'public', 'collections');
const CHECK = process.argv.includes('--check');
const SCHEMA = 'https://schema.getpostman.com/json/collection/v2.1.0/collection.json';

const skipStateChange = [
  "if (pm.collectionVariables.get('enableStateChangingRequests') !== 'true') {",
  "  pm.execution.skipRequest();",
  '}',
];

const skipStream = [
  "if (pm.collectionVariables.get('enableStreamingRequests') !== 'true') {",
  "  pm.execution.skipRequest();",
  '}',
];

const jsonHeaders = [{ key: 'Content-Type', value: 'application/json', type: 'text' }];

function variable(key, value = '', sensitive = false, description = '') {
  return { key, value, type: 'string', ...(sensitive ? { description: `Sensitive. ${description}`.trim() } : description ? { description } : {}) };
}

function tests(statuses = [200], format = 'json', capture) {
  const lines = [
    "pm.test('Status code is expected', () => {",
    `  pm.expect(pm.response.code).to.be.oneOf(${JSON.stringify(statuses)});`,
    '});',
    "pm.test('Response time is below 5 seconds', () => {",
    '  pm.expect(pm.response.responseTime).to.be.below(5000);',
    '});',
  ];
  if (format === 'json') {
    lines.push(
      "pm.test('Response is JSON when it has a body', () => {",
      '  if (pm.response.text().length > 0) {',
      "    pm.expect(pm.response.headers.get('Content-Type') || '').to.include('application/json');",
      '    pm.response.json();',
      '  }',
      '});',
    );
  } else if (format) {
    lines.push(
      `pm.test('Response Content-Type is ${format}', () => {`,
      '  if (pm.response.code >= 200 && pm.response.code < 300) {',
      `    pm.expect(pm.response.headers.get('Content-Type') || '').to.include('${format}');`,
      '  }',
      '});',
    );
  }
  if (capture) {
    lines.push(
      `if (${JSON.stringify(statuses)}.includes(pm.response.code)) {`,
      '  const body = pm.response.json();',
      `  if (body[${JSON.stringify(capture.field)}]) pm.collectionVariables.set(${JSON.stringify(capture.variable)}, body[${JSON.stringify(capture.field)}]);`,
      '}',
    );
  }
  return [{ listen: 'test', script: { type: 'text/javascript', exec: lines } }];
}

function req(name, method, path, options = {}) {
  const request = {
    method,
    header: options.body === undefined ? [] : [...jsonHeaders],
    url: `{{${options.baseVariable ?? 'baseUrl'}}}${path}`,
    description: options.description ?? `${method} ${path}`,
  };
  if (options.headers) request.header.push(...options.headers);
  if (options.body !== undefined) {
    request.body = { mode: 'raw', raw: JSON.stringify(options.body, null, 2), options: { raw: { language: 'json' } } };
  }
  const event = tests(options.statuses ?? [200], options.format === undefined ? 'json' : options.format, options.capture);
  if (options.stateChanging) event.unshift({ listen: 'prerequest', script: { type: 'text/javascript', exec: skipStateChange } });
  if (options.streaming) event.unshift({ listen: 'prerequest', script: { type: 'text/javascript', exec: skipStream } });
  return { name, event, request };
}

function folder(name, item, description) {
  return { name, ...(description ? { description } : {}), item };
}

function apiKeyAuth(keyVariable, header = 'X-API-Key') {
  return {
    type: 'apikey',
    apikey: [
      { key: 'key', value: header, type: 'string' },
      { key: 'value', value: `{{${keyVariable}}}`, type: 'string' },
      { key: 'in', value: 'header', type: 'string' },
    ],
  };
}

function collection(name, description, variables, items, auth) {
  return {
    info: { name, description, schema: SCHEMA },
    ...(auth ? { auth } : {}),
    variable: [
      ...variables,
      variable('enableStateChangingRequests', 'false', false, 'Explicit opt-in for requests that place orders, send messages, control processes, or write data.'),
      variable('enableStreamingRequests', 'false', false, 'Explicit opt-in for long-lived SSE requests.'),
    ],
    item: items,
  };
}

const executionVariables = [
  variable('baseUrl', 'http://localhost:8010'),
  variable('apiKey', '', true, 'Execution service API key.'),
  variable('symbol', 'XAUUSD'),
  variable('timeframe', 'M15'),
  variable('accountAlias', 'demo'),
  variable('signalSource', 'ipda'),
  variable('operationId', '00000000-0000-0000-0000-000000000001'),
  variable('signalId', '00000000-0000-0000-0000-000000000002'),
  variable('orderId', '1'),
  variable('positionId', '1'),
];

const operationBase = { operation_id: '{{$guid}}', occurred_at: '{{$isoTimestamp}}', source: 'postman' };
const executionItems = [
  folder('Health', [
    req('Liveness', 'GET', '/health/live'),
    req('Readiness', 'GET', '/health/ready', { statuses: [200, 503] }),
    req('Trading readiness', 'GET', '/health/trading-ready', { statuses: [200, 503] }),
  ]),
  folder('Market data — cTrader adapter', [
    req('Tick', 'GET', '/v1/market-data/tick?symbol={{symbol}}&account={{accountAlias}}', { statuses: [200, 404, 503] }),
    req('Candles', 'GET', '/v1/market-data/candles?symbol={{symbol}}&timeframe={{timeframe}}&count=100&account={{accountAlias}}', { statuses: [200, 404, 503] }),
    req('Symbols', 'GET', '/v1/symbols?account={{accountAlias}}', { statuses: [200, 404, 503] }),
    req('Tick stream', 'GET', '/v1/stream/ticks?symbols={{symbol}}&account={{accountAlias}}', { streaming: true, format: 'text/event-stream', statuses: [200] }),
  ]),
  folder('Orders and positions', [
    req('Place market order', 'POST', '/v1/orders', { stateChanging: true, statuses: [201, 202], capture: { field: 'operation_id', variable: 'operationId' }, body: { ...operationBase, instrument: '{{symbol}}', execution_type: 'market', direction: 'buy', targets: [{ account: '{{accountAlias}}', volume_lots: '0.01' }], stop_loss_distance: '10', take_profit_distance: '20', time_in_force: 'gtc', note: 'Postman endpoint test' } }),
    req('Amend order', 'POST', '/v1/orders/amend', { stateChanging: true, statuses: [201, 202], capture: { field: 'operation_id', variable: 'operationId' }, body: { ...operationBase, targets: [{ account: '{{accountAlias}}', order_id: '{{orderId}}', stop_loss: '1' }] } }),
    req('Cancel order', 'POST', '/v1/orders/cancel', { stateChanging: true, statuses: [201, 202], capture: { field: 'operation_id', variable: 'operationId' }, body: { ...operationBase, targets: [{ account: '{{accountAlias}}', order_id: '{{orderId}}' }] } }),
    req('Amend position protection', 'POST', '/v1/positions/protection', { stateChanging: true, statuses: [201, 202], capture: { field: 'operation_id', variable: 'operationId' }, body: { ...operationBase, targets: [{ account: '{{accountAlias}}', position_id: '{{positionId}}', stop_loss: '1', trailing_stop_loss: false }] } }),
    req('Close position', 'POST', '/v1/positions/close', { stateChanging: true, statuses: [201, 202], capture: { field: 'operation_id', variable: 'operationId' }, body: { ...operationBase, targets: [{ account: '{{accountAlias}}', position_id: '{{positionId}}', volume_lots: '0.01' }] } }),
    req('Operation status', 'GET', '/v1/operations/{{operationId}}', { statuses: [200, 404] }),
    req('Account orders', 'GET', '/v1/accounts/{{accountAlias}}/orders', { statuses: [200, 404, 503] }),
    req('Account positions', 'GET', '/v1/accounts/{{accountAlias}}/positions', { statuses: [200, 404, 503] }),
  ]),
  folder('MT5 compatibility adapter', [
    req('Submit signal', 'POST', '/v1/signals', { stateChanging: true, statuses: [200, 201], capture: { field: 'signal_id', variable: 'signalId' }, body: { signal_id: '{{$guid}}', occurred_at: '{{$isoTimestamp}}', execution_type: 'market', symbol: '{{symbol}}', direction: 'buy', volume: '0.01', stop_loss_distance: '10', take_profit_distance: '20', source: '{{signalSource}}', ignore_signal_age: true } }),
    req('Signal status', 'GET', '/v1/signals/{{signalId}}', { statuses: [200, 404] }),
    req('Legacy candles', 'GET', '/v1/market-data/candles?quote={{symbol}}&timeframe={{timeframe}}&count=100', { statuses: [200, 404, 503] }),
    req('Legacy tick', 'GET', '/v1/market-data/tick?quote={{symbol}}', { statuses: [200, 404, 503] }),
  ]),
];

const backtestingItems = [
  folder('Health', [req('Liveness', 'GET', '/health/live'), req('Readiness', 'GET', '/health/ready', { statuses: [200, 503] })]),
  folder('Data and configuration', [
    req('Candles', 'GET', '/v1/candles?symbol={{symbol}}&timeframe={{timeframe}}&source={{source}}&count=100', { statuses: [200, 404, 503] }),
    req('Configuration', 'GET', '/v1/config'),
    req('Paper status', 'GET', '/v1/paper'),
    req('Execution status', 'GET', '/v1/execution', { statuses: [200, 503] }),
    req('S7 prop-guard Monte Carlo artifact', 'GET', '/v1/research/s7-propguard-monte-carlo', { statuses: [200, 404] }),
  ]),
  folder('Backtests', [
    req('Run backtest', 'POST', '/v1/backtests', { statuses: [200, 404, 503], body: { strategy: 'session_hedge', symbol: '{{symbol}}', timeframe: '{{timeframe}}', source: '{{source}}' } }),
    req('Compare entry modes', 'POST', '/v1/backtests/compare', { statuses: [200, 404, 503], body: { strategy: 'session_hedge', symbol: '{{symbol}}', timeframe: '{{timeframe}}', source: '{{source}}' } }),
  ]),
];

const notificationItems = [
  folder('Notifications', [
    req('Send notification', 'POST', '/notifications', { stateChanging: true, statuses: [200, 201], capture: { field: 'id', variable: 'notificationId' }, body: { idempotencyKey: 'postman-{{$guid}}', subject: 'Postman endpoint test', message: 'Postman endpoint test', contentType: 'text', channels: ['TELEGRAM'], source: 'postman' } }),
    req('List notifications', 'GET', '/notifications?limit=100'),
    req('Get notification', 'GET', '/notifications/{{notificationId}}', { statuses: [200, 404] }),
  ]),
  folder('WhatsApp webhooks', [
    req('Verify webhook', 'GET', '/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token={{whatsappVerifyToken}}&hub.challenge={{webhookChallenge}}', { statuses: [200, 403], format: 'text/plain' }),
    req('Receive webhook status', 'POST', '/webhooks/whatsapp', { stateChanging: true, statuses: [200, 401], headers: [{ key: 'X-Hub-Signature-256', value: '{{whatsappSignature}}', type: 'text' }], body: { object: 'whatsapp_business_account', entry: [] } }),
  ]),
];

const mt5Items = [
  folder('Health', [req('Liveness', 'GET', '/health/live'), req('Readiness', 'GET', '/health/ready', { statuses: [200, 503] })]),
  folder('Signals', [
    req('Submit signal', 'POST', '/v1/signals', { stateChanging: true, statuses: [200, 201], capture: { field: 'signal_id', variable: 'signalId' }, body: { signal_id: '{{$guid}}', occurred_at: '{{$isoTimestamp}}', execution_type: 'market', symbol: '{{symbol}}', direction: 'buy', volume: '0.01', stop_loss_distance: '10', take_profit_distance: '20', source: '{{signalSource}}', ignore_signal_age: true } }),
    req('Signal status', 'GET', '/v1/signals/{{signalId}}', { statuses: [200, 404] }),
  ]),
  folder('Market data', [
    req('Candles', 'GET', '/v1/market-data/candles?quote={{symbol}}&timeframe={{timeframe}}&count=100', { statuses: [200, 404, 503] }),
    req('Tick', 'GET', '/v1/market-data/tick?quote={{symbol}}', { statuses: [200, 404, 503] }),
  ]),
];

const lookupReadStatuses = [200, 404, 409, 503];
const lookupItems = [
  folder('Health and catalog', [
    req('Health', 'GET', '/health'), req('Data/model health', 'GET', '/health/data-model'), req('Symbols', 'GET', '/symbols'),
    req('Timeframes', 'GET', '/timeframes?symbol={{symbol}}'), req('Setups', 'GET', '/setups'),
  ]),
  folder('Candles and context', [
    req('Candle bounds', 'GET', '/candles/bounds?symbol={{symbol}}&timeframe={{timeframe}}'),
    req('Candles', 'GET', '/candles?symbol={{symbol}}&timeframe={{timeframe}}&date_from={{dateFrom}}&date_to={{dateTo}}', { statuses: lookupReadStatuses }),
    req('Candle page', 'GET', '/candles/page?symbol={{symbol}}&timeframe={{timeframe}}&date_from={{dateFrom}}&date_to={{dateTo}}&offset=0&limit=100', { statuses: lookupReadStatuses }),
    req('Context', 'GET', '/context?symbol={{symbol}}&timeframe={{timeframe}}&signal_ts={{signalTimestamp}}', { statuses: lookupReadStatuses }),
    req('Bar feature series', 'GET', '/bar-features/series?symbol={{symbol}}&timeframe={{timeframe}}&date_from={{dateFrom}}&date_to={{dateTo}}&horizon=24', { statuses: lookupReadStatuses }),
    req('Base rate', 'GET', '/base-rate?symbol={{symbol}}&timeframe={{timeframe}}&signal_ts={{signalTimestamp}}&horizon=24&target_atr=1.5&stop_atr=1.0', { statuses: [200, 400, 404, 503] }),
  ]),
  folder('Sessions, labels, and signals', [
    req('Create session', 'POST', '/sessions', { stateChanging: true, capture: { field: 'session_id', variable: 'sessionId' }, body: { symbol: '{{symbol}}', timeframe: '{{timeframe}}', date_from: '{{dateFrom}}', date_to: '{{dateTo}}', blinded: true, notes: 'Postman endpoint test' } }),
    req('Patch session', 'PATCH', '/sessions/{{sessionId}}', { stateChanging: true, statuses: [200, 404], body: { ended_at: '{{$isoTimestamp}}', notes: 'Postman endpoint test completed' } }),
    req('Create trade', 'POST', '/trades', { stateChanging: true, statuses: [200, 400], capture: { field: 'id', variable: 'occurrenceId' }, body: { session_id: '{{sessionId}}', symbol: '{{symbol}}', timeframe: '{{timeframe}}', signal_ts: '{{signalTimestamp}}', setup_id: '{{setupId}}', side: 1, entry: 2000, sl: 1990, tp: 2020, notes: 'Postman endpoint test' } }),
    req('List trades', 'GET', '/trades?session_id={{sessionId}}'),
    req('Patch trade', 'PATCH', '/trades/{{occurrenceId}}', { stateChanging: true, statuses: [200, 404], body: { notes: 'Updated by Postman endpoint test' } }),
    req('Delete trade (soft exclusion)', 'DELETE', '/trades/{{occurrenceId}}?reason=postman_test', { stateChanging: true, statuses: [200, 404] }),
    req('Create signal', 'POST', '/signals', { stateChanging: true, statuses: [200, 400], capture: { field: 'id', variable: 'lookupSignalId' }, body: { session_id: '{{sessionId}}', symbol: '{{symbol}}', timeframe: '{{timeframe}}', signal_ts: '{{signalTimestamp}}', setup_id: '{{setupId}}', side: 1, blinded: true } }),
    req('List signals', 'GET', '/signals?session_id={{sessionId}}'),
    req('Resolve pending signals', 'POST', '/signals/resolve-pending', { stateChanging: true }),
    req('Compare setup', 'POST', '/compare', { statuses: [200, 404, 503], body: { setup_id: '{{setupId}}', symbol: '{{symbol}}', timeframe: '{{timeframe}}', context: { trend_state: 'up', atr_bucket: 'medium', session: 'london', rsi_band: 'neutral' }, source: 'manual' } }),
  ]),
  folder('Screenshots and exports', [
    req('Upload screenshot', 'POST', '/screenshots', { stateChanging: true, capture: { field: 'path', variable: 'screenshotPath' }, body: { session_id: '{{sessionId}}', trade_id: '{{occurrenceId}}', kind: 'entry', image_base64: '{{onePixelPngBase64}}' } }),
    req('Get screenshot', 'GET', '/screenshots/{{sessionId}}/{{screenshotFilename}}', { statuses: [200, 404], format: 'image/png' }),
    req('Export occurrences', 'GET', '/export?format=csv', { stateChanging: true, statuses: [200, 404], format: 'text/csv' }),
    req('Export bar features', 'GET', '/export/bar-features?symbol={{symbol}}&timeframe={{timeframe}}&date_from={{dateFrom}}&date_to={{dateTo}}&format=csv', { stateChanging: true, statuses: [200, 404], format: 'text/csv' }),
    req('Export meta shadow', 'GET', '/export/meta-shadow?symbol={{symbol}}&timeframe={{timeframe}}&forward_only=true', { stateChanging: true, statuses: [200, 404], format: 'text/csv' }),
  ]),
  folder('Outcome and meta models', [
    req('Outcome shadow', 'GET', '/outcome-model/shadow?symbol={{symbol}}&timeframe={{timeframe}}&signal_ts={{signalTimestamp}}', { statuses: lookupReadStatuses }),
    req('Outcome shadow history', 'GET', '/outcome-model/shadow/history?symbol={{symbol}}&timeframe={{timeframe}}&date_from={{dateFrom}}&date_to={{dateTo}}', { statuses: lookupReadStatuses }),
    req('Meta-model replay', 'GET', '/meta-model/replay?symbol={{symbol}}&timeframe={{timeframe}}&signal_ts={{signalTimestamp}}&side=1', { statuses: lookupReadStatuses }),
    req('Meta-model shadow', 'GET', '/meta-model/shadow?symbol={{symbol}}&timeframe={{timeframe}}&signal_ts={{signalTimestamp}}', { statuses: lookupReadStatuses }),
    req('Meta-model shadow history', 'GET', '/meta-model/shadow/history?symbol={{symbol}}&timeframe={{timeframe}}&offset=0&limit=100', { statuses: lookupReadStatuses }),
    req('Meta-model status', 'GET', '/meta-model/status'),
  ]),
  folder('Meta events', [
    req('List meta events', 'GET', '/meta-events?symbol={{symbol}}&timeframe={{timeframe}}&offset=0&limit=100'),
    req('Meta-event summary', 'GET', '/meta-events/summary?symbol={{symbol}}&timeframe={{timeframe}}'),
    req('Meta-event detail', 'GET', '/meta-events/{{metaEventId}}', { statuses: [200, 404] }),
    req('Review meta event', 'POST', '/meta-events/{{metaEventId}}/review', { stateChanging: true, statuses: [200, 404, 409], body: { verdict: 'valid', notes: 'Postman endpoint test', phase: 'pre' } }),
    req('Reveal meta event', 'POST', '/meta-events/{{metaEventId}}/reveal', { stateChanging: true, statuses: [200, 404, 409] }),
  ]),
  folder('Calendar', [
    req('Calendar events', 'GET', '/calendar/events?date={{calendarDate}}', { statuses: [200, 503] }),
    req('Calendar flags', 'GET', '/calendar/flags?symbol={{symbol}}&ts={{signalTimestamp}}', { statuses: [200, 503] }),
  ]),
];

const pumpPaths = [
  ['Health', '/api/health'], ['Dashboard summary', '/api/dashboard/summary?track={{track}}'],
  ['Positions', '/api/positions?state=all&limit=100&track={{track}}'], ['Shadow outcomes', '/api/shadow-outcomes?range={{range}}&limit=100'],
  ['PnL', '/api/pnl?range={{range}}&track={{track}}'], ['Candidates', '/api/candidates?limit=100'],
  ['Events', '/api/events?limit=100&level=info'], ['Risk status', '/api/risk/status'], ['Breakers', '/api/breakers?limit=100'],
  ['Latency', '/api/latency?kind=detection&range={{range}}'], ['Ops analytics', '/api/analytics/ops'],
  ['Performance analytics', '/api/analytics/performance?range={{range}}&track={{track}}'],
  ['Execution drag', '/api/analytics/execution-drag?range={{range}}&limit=100&allSessions=0'],
  ['Funnel', '/api/analytics/funnel?range={{range}}'], ['Veto reasons', '/api/analytics/veto-reasons?range={{range}}'],
  ['Shadow veto quality', '/api/analytics/shadow-veto-quality?range={{range}}'], ['Shadow coverage', '/api/analytics/shadow-coverage?range={{range}}'],
  ['Veto/dry-run comparison', '/api/analytics/veto-dry-run-comparison?range={{range}}'], ['Relaxed risk', '/api/analytics/relaxed-risk?range={{range}}'],
  ['Trades CSV', '/api/reports/trades.csv?range={{range}}&track={{track}}', 'text/csv'],
  ['Execution drag CSV', '/api/reports/execution-drag.csv?range={{range}}&allSessions=0', 'text/csv'],
  ['Ops report', '/api/reports/ops.json'], ['Soak report', '/api/reports/soak.json?range={{range}}'],
  ['Funnel CSV', '/api/reports/funnel.csv?range={{range}}', 'text/csv'], ['Veto/dry-run CSV', '/api/reports/veto-dry-run.csv?range={{range}}', 'text/csv'],
  ['Veto/dry-run summary CSV', '/api/reports/veto-dry-run-summary.csv?range={{range}}', 'text/csv'],
  ['Strategy week JSON', '/api/reports/strategy-week.json?range={{range}}&allowMixed=0'],
  ['Strategy week Markdown', '/api/reports/strategy-week.md?range={{range}}&allowMixed=0', 'text/markdown'],
  ['Strategy trades CSV', '/api/reports/strategy-trades.csv?range={{range}}&allowMixed=0', 'text/csv'],
  ['Strategy fills CSV', '/api/reports/strategy-fills.csv?range={{range}}', 'text/csv'],
  ['Public config', '/api/config-public'],
];
const pumpItems = [
  folder('Dashboard API', pumpPaths.filter(([, path]) => !path.startsWith('/api/reports/')).map(([name, path, format]) => req(name, 'GET', path, { format }))),
  folder('Reports', pumpPaths.filter(([, path]) => path.startsWith('/api/reports/')).map(([name, path, format]) => req(name, 'GET', path, { format }))),
  folder('Streaming', [req('Event stream', 'GET', '/api/stream', { streaming: true, format: 'text/event-stream' })]),
];

const forexItems = [
  folder('Health', [req('Liveness', 'GET', '/health/live'), req('Readiness', 'GET', '/health/ready', { statuses: [200, 503] })]),
  folder('Account', [
    req('Summary', 'GET', '/api/v1/account/summary'), req('Details', 'GET', '/api/v1/account/details'),
    req('Instruments', 'GET', '/api/v1/account/instruments'), req('Snapshots', 'GET', '/api/v1/account/snapshots?limit=25'),
    req('Changes', 'GET', '/api/v1/account/changes?sinceTransactionId={{sinceTransactionId}}'),
  ]),
];

const fuItems = [
  folder('Service', [req('Root', 'GET', '/'), req('Health', 'GET', '/health')]),
  folder('Notifications', [
    req('List notifications', 'GET', '/notifications?limit=100'),
    req('Get notification', 'GET', '/notifications/{{notificationId}}', { statuses: [200, 404] }),
    req('Send test notification', 'POST', '/notifications/test', { stateChanging: true, statuses: [200, 503], body: { recipient: '{{notificationRecipient}}', message: 'Postman endpoint test' } }),
    req('Broadcast test notification', 'POST', '/notifications/test/broadcast', { stateChanging: true, statuses: [200, 400, 503], body: { message: 'Postman endpoint test' } }),
  ]),
  folder('Webhooks', [
    req('Verify WhatsApp webhook', 'GET', '/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token={{whatsappVerifyToken}}&hub.challenge={{webhookChallenge}}', { statuses: [200, 403], format: 'text/plain' }),
    req('Receive WhatsApp webhook', 'POST', '/webhooks/whatsapp', { stateChanging: true, statuses: [200, 401], headers: [{ key: 'X-Hub-Signature-256', value: '{{whatsappSignature}}', type: 'text' }], body: { object: 'whatsapp_business_account', entry: [] } }),
    req('Receive TradingView webhook', 'POST', '/webhooks/tradingview?token={{tradingviewWebhookToken}}', { stateChanging: true, statuses: [200, 403], body: { symbol: '{{symbol}}', timeframe: '1m', signal: 'Bullish reversal', price: 2000, indicator: 'Postman endpoint test' } }),
  ]),
];

const vrvpItems = [
  folder('Read', [
    req('Root', 'GET', '/'), req('Health', 'GET', '/health'), req('Status', 'GET', '/status', { statuses: [200, 503] }),
    req('Pairs', 'GET', '/pairs', { statuses: [200, 503] }), req('Pair status', 'GET', '/pairs/{{instrument}}', { statuses: [200, 404, 503] }),
    req('Signals', 'GET', '/signals', { statuses: [200, 503] }), req('Pair signal', 'GET', '/signals/{{instrument}}', { statuses: [200, 404, 503] }),
  ]),
  folder('Controls', [
    req('Restart strategy', 'POST', '/restart', { stateChanging: true, statuses: [200, 500, 503] }),
    req('Stop strategy', 'POST', '/stop', { stateChanging: true, statuses: [200, 503] }),
    req('Start strategy', 'POST', '/start', { stateChanging: true, statuses: [200, 500, 503] }),
  ]),
];

const collections = new Map([
  ['execution-service.postman_collection.json', collection('Trading Algos — Execution Service', 'All execution-service endpoints, including cTrader and MT5 compatibility adapter routes.', executionVariables, executionItems, apiKeyAuth('apiKey'))],
  ['backtesting-service.postman_collection.json', collection('Trading Algos — Backtesting Service', 'All backtesting-service endpoints.', [variable('baseUrl', 'http://localhost:8012'), variable('apiKey', '', true, 'Optional backtesting API key.'), variable('symbol', 'XAUUSD'), variable('timeframe', 'M15'), variable('source', 'local')], backtestingItems, apiKeyAuth('apiKey'))],
  ['notification-service.postman_collection.json', collection('Trading Algos — Notification Service', 'All notification-service endpoints.', [variable('baseUrl', 'http://localhost:3001'), variable('apiKey', '', true, 'Notification service API key.'), variable('notificationId', 'replace-with-notification-id'), variable('whatsappVerifyToken', '', true, 'WhatsApp webhook verification token.'), variable('whatsappSignature', '', true, 'Computed sha256 webhook signature.'), variable('webhookChallenge', 'postman-challenge')], notificationItems, apiKeyAuth('apiKey'))],
  ['mt5-trader.postman_collection.json', collection('Trading Algos — MT5 Trader (Frozen)', 'All endpoints on the frozen pre-cutover MT5 service.', [variable('baseUrl', 'http://localhost:8000'), variable('apiKey', '', true, 'MT5 service API key.'), variable('symbol', 'XAUUSD'), variable('timeframe', 'M1'), variable('signalSource', 'ipda'), variable('signalId', '00000000-0000-0000-0000-000000000002')], mt5Items, apiKeyAuth('apiKey'))],
  ['lookup-trader.postman_collection.json', collection('Trading Algos — Lookup Trader', 'All lookup-trader server endpoints.', [variable('baseUrl', 'http://localhost:8000'), variable('symbol', 'XAUUSD'), variable('timeframe', 'H1'), variable('dateFrom', '2025-01-01T00:00:00Z'), variable('dateTo', '2025-01-31T23:00:00Z'), variable('signalTimestamp', '2025-01-15T12:00:00Z'), variable('calendarDate', '2025-01-15'), variable('setupId', 'breakout'), variable('sessionId', 'replace-with-session-id'), variable('occurrenceId', 'replace-with-occurrence-id'), variable('lookupSignalId', 'replace-with-signal-id'), variable('metaEventId', 'replace-with-meta-event-id'), variable('screenshotFilename', 'replace-with-screenshot-filename.png'), variable('screenshotPath', ''), variable('onePixelPngBase64', 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')], lookupItems)],
  ['pump-fun.postman_collection.json', collection('Trading Algos — Pump.fun Dashboard', 'All pump-fun dashboard API endpoints.', [variable('baseUrl', 'http://localhost:3000'), variable('dashboardUsername', '', true, 'Dashboard Basic Auth username.'), variable('dashboardPassword', '', true, 'Dashboard Basic Auth password.'), variable('range', '7d'), variable('track', 'live')], pumpItems, { type: 'basic', basic: [{ key: 'username', value: '{{dashboardUsername}}', type: 'string' }, { key: 'password', value: '{{dashboardPassword}}', type: 'string' }] })],
  ['forex-execution.postman_collection.json', collection('Trading Algos — Forex Execution', 'All forex-execution endpoints.', [variable('baseUrl', 'http://localhost:3000'), variable('internalApiKey', '', true, 'Internal API key.'), variable('sinceTransactionId', '1')], forexItems, apiKeyAuth('internalApiKey', 'X-Internal-API-Key'))],
  ['fu-strategy.postman_collection.json', collection('Trading Algos — FU Strategy', 'All FU Strategy endpoints.', [variable('baseUrl', 'http://localhost:8000'), variable('symbol', 'GOLD'), variable('notificationId', 'replace-with-notification-id'), variable('notificationRecipient', '', true, 'Phone number or recipient supported by the configured channel.'), variable('whatsappVerifyToken', '', true, 'WhatsApp webhook verification token.'), variable('whatsappSignature', '', true, 'Computed sha256 webhook signature.'), variable('tradingviewWebhookToken', '', true, 'TradingView webhook token.'), variable('webhookChallenge', 'postman-challenge')], fuItems)],
  ['vrvp-strategy.postman_collection.json', collection('Trading Algos — VRVP Strategy', 'All VRVP Strategy API endpoints.', [variable('baseUrl', 'http://localhost:8000'), variable('instrument', 'EURUSD')], vrvpItems)],
  ['telegram-bot.postman_collection.json', collection('Trading Algos — Telegram Bot', 'All Telegram Bot HTTP endpoints.', [variable('baseUrl', 'http://localhost:3000')], [folder('Service', [req('Health', 'GET', '/health'), req('Status', 'GET', '/status')])])],
]);

function countRequests(items) {
  return items.reduce((total, item) => total + (item.request ? 1 : countRequests(item.item ?? [])), 0);
}

function validate(name, document) {
  JSON.parse(JSON.stringify(document));
  const requests = [];
  const visit = (items) => items.forEach((item) => item.request ? requests.push(item) : visit(item.item ?? []));
  visit(document.item);
  if (requests.length === 0) throw new Error(`${name}: collection has no requests`);
  for (const item of requests) {
    if (!item.event?.some((event) => event.listen === 'test' && event.script?.exec?.length)) throw new Error(`${name}: ${item.name} has no tests`);
    const raw = typeof item.request.url === 'string' ? item.request.url : item.request.url.raw;
    if (!raw.startsWith('{{')) throw new Error(`${name}: ${item.name} has a literal URL`);
  }
  const serialized = JSON.stringify(document);
  const declaredVariables = new Set(document.variable.map((entry) => entry.key));
  const references = [...serialized.matchAll(/\{\{([^{}]+)\}\}/g)].map((match) => match[1]);
  for (const reference of references) {
    if (!reference.startsWith('$') && !declaredVariables.has(reference)) {
      throw new Error(`${name}: undeclared collection variable {{${reference}}}`);
    }
  }
  for (const entry of document.variable) {
    if (entry.description?.startsWith('Sensitive.') && entry.value !== '') {
      throw new Error(`${name}: sensitive variable ${entry.key} must have an empty committed value`);
    }
  }
  for (const forbidden of ['Bearer ey', 'sk-', 'ghp_', 'xoxb-']) {
    if (serialized.includes(forbidden)) throw new Error(`${name}: possible literal credential (${forbidden})`);
  }
  return requests.length;
}

await mkdir(OUTPUT, { recursive: true });
const expectedNames = new Set(collections.keys());
const existingNames = new Set((await readdir(OUTPUT)).filter((name) => name.endsWith('.postman_collection.json')));
for (const stale of existingNames) {
  if (!expectedNames.has(stale)) throw new Error(`Unexpected stale collection: ${stale}`);
}

for (const [name, document] of collections) {
  const count = validate(name, document);
  const content = `${JSON.stringify(document, null, 2)}\n`;
  const path = join(OUTPUT, name);
  if (CHECK) {
    let current;
    try { current = await readFile(path, 'utf8'); } catch { throw new Error(`${name} is missing; run npm run postman:generate`); }
    if (current !== content) throw new Error(`${name} is stale; run npm run postman:generate`);
  } else {
    await writeFile(path, content, 'utf8');
  }
  console.log(`${CHECK ? 'verified' : 'generated'} ${name} (${count} requests)`);
}
