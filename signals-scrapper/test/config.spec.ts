import {
  assertRuntimeConfig,
  detectHostOs,
  isLoopbackCdpEndpoint,
  loadAppConfig,
  parseMt5SignalRules,
  parseSources,
  resolveHostOs,
  SourcesSchema,
} from '../src/config/sources.schema';

describe('config / SOURCES validation', () => {
  const validSources = JSON.stringify([
    {
      type: 'TRADING_CENTRAL',
      url: 'https://secure.icmarkets.com/TradingCentral/TradingCentral',
    },
    {
      type: 'AUTOCHARTIST',
      url: 'https://secure.icmarkets.com/AutoChartist/AutoChartist',
    },
  ]);

  it('parses a valid SOURCES JSON array with both provider types', () => {
    const sources = parseSources(validSources);
    expect(sources).toHaveLength(2);
    expect(sources[0].type).toBe('TRADING_CENTRAL');
    expect(sources[1].type).toBe('AUTOCHARTIST');
    expect(sources[0].url).toMatch(/^https:\/\//);
  });

  it('rejects missing SOURCES', () => {
    expect(() => parseSources(undefined)).toThrow(/SOURCES environment variable is required/);
    expect(() => parseSources('')).toThrow(/SOURCES environment variable is required/);
  });

  it('rejects invalid JSON', () => {
    expect(() => parseSources('not-json')).toThrow(/not valid JSON/);
  });

  it('rejects unknown provider type (fail-fast, not skip)', () => {
    const bad = JSON.stringify([
      {
        type: 'SOME_OTHER_VENDOR',
        url: 'https://example.com/x',
      },
    ]);
    expect(() => parseSources(bad)).toThrow();
    expect(() => SourcesSchema.parse(JSON.parse(bad))).toThrow(/TRADING_CENTRAL|AUTOCHARTIST|enum/i);
  });

  it('rejects empty array', () => {
    expect(() => parseSources('[]')).toThrow();
  });

  it('rejects non-URL values', () => {
    const bad = JSON.stringify([{ type: 'TRADING_CENTRAL', url: 'not-a-url' }]);
    expect(() => parseSources(bad)).toThrow();
  });

  it('loadAppConfig returns full typed config from env', () => {
    const cfg = loadAppConfig({
      SOURCES: validSources,
      BROWSER_MODE: 'PERSISTENT',
      USER_DATA_DIR: './.chrome-profile',
      SIGNAL_CRON_EXPRESSION: '0 * * * *',
      AUTH_REFRESH_CRON_EXPRESSION: '*/5 * * * *',
      IDEAS_LOG_PATH: './data/ideas.jsonl',
      SCREENSHOT_DIR: './data/screenshots',
      SEEN_STATE_PATH: './data/seen.json',
      HEADLESS: 'false',
      NAV_TIMEOUT_MS: '30000',
    });
    expect(cfg.BROWSER_MODE).toBe('PERSISTENT');
    expect(cfg.USER_DATA_DIR).toBe('./.chrome-profile');
    expect(cfg.SIGNAL_CRON_EXPRESSION).toBe('0 * * * *');
    expect(cfg.AUTH_REFRESH_CRON_EXPRESSION).toBe('*/5 * * * *');
    expect(cfg.NAV_TIMEOUT_MS).toBe(30000);
    expect(cfg.HEADLESS).toBe(false);
    expect(cfg.SOURCES).toHaveLength(2);
    expect(cfg.HOST_OS).toBe('AUTO');
    expect(cfg.CDP_AUTO_START).toBe(true);
    expect(cfg.CDP_STARTUP_TIMEOUT_MS).toBe(20000);
  });

  it('defaults BROWSER_MODE to PERSISTENT when unset', () => {
    const cfg = loadAppConfig({ SOURCES: validSources });
    expect(cfg.BROWSER_MODE).toBe('PERSISTENT');
    expect(cfg.USER_DATA_DIR).toBe('./.chrome-profile');
    expect(cfg.MT5_SIGNAL_TRADING_ENABLED).toBe(false);
    expect(cfg.MT5_SIGNAL_API_URL).toBe('http://127.0.0.1:8000');
    expect(cfg.MT5_SIGNAL_TIMEOUT_MS).toBe(70000);
    expect(cfg.MT5_SIGNAL_RULES).toEqual({});
    expect(cfg.OPENAI_MODEL).toBe('gpt-5.6-luna');
    expect(cfg.OPENAI_TIMEOUT_MS).toBe(60000);
    expect(cfg.SIGNAL_CRON_EXPRESSION).toBe('*/15 * * * *');
    expect(cfg.AUTH_REFRESH_CRON_EXPRESSION).toBe('*/5 * * * *');
  });

  it('uses legacy CRON_EXPRESSION as the signal schedule fallback', () => {
    const cfg = loadAppConfig({
      SOURCES: validSources,
      CRON_EXPRESSION: '30 * * * *',
    });

    expect(cfg.SIGNAL_CRON_EXPRESSION).toBe('30 * * * *');
  });

  it('loadAppConfig rejects invalid BROWSER_MODE', () => {
    expect(() =>
      loadAppConfig({
        SOURCES: validSources,
        BROWSER_MODE: 'PUPPETEER',
      }),
    ).toThrow();
  });

  it('loadAppConfig fails when SOURCES has bad type', () => {
    expect(() =>
      loadAppConfig({
        SOURCES: JSON.stringify([
          { type: 'UNKNOWN', url: 'https://example.com' },
        ]),
      }),
    ).toThrow();
  });

  it('requires CDP and an OpenAI API key for Trading Central at runtime', () => {
    const persistent = loadAppConfig({
      SOURCES: validSources,
      BROWSER_MODE: 'PERSISTENT',
      OPENAI_API_KEY: 'key',
    });
    expect(() => assertRuntimeConfig(persistent)).toThrow(/requires BROWSER_MODE=CDP/);

    const missingKey = loadAppConfig({
      SOURCES: validSources,
      BROWSER_MODE: 'CDP',
    });
    expect(() => assertRuntimeConfig(missingKey)).toThrow(/OPENAI_API_KEY/);

    const valid = loadAppConfig({
      SOURCES: validSources,
      BROWSER_MODE: 'CDP',
      CDP_AUTO_START: 'false',
      OPENAI_API_KEY: 'key',
    });
    expect(() => assertRuntimeConfig(valid)).not.toThrow();
  });

  it('detects macOS/Windows and enforces explicit HOST_OS assertions', () => {
    expect(detectHostOs('darwin')).toBe('MACOS');
    expect(detectHostOs('win32')).toBe('WINDOWS');
    expect(detectHostOs('linux')).toBeUndefined();
    expect(resolveHostOs('AUTO', 'darwin')).toBe('MACOS');
    expect(resolveHostOs('WINDOWS', 'win32')).toBe('WINDOWS');
    expect(() => resolveHostOs('WINDOWS', 'darwin')).toThrow(/does not match/);
    expect(() => resolveHostOs('AUTO', 'linux')).toThrow(/unsupported/);

    const detected = detectHostOs();
    const mismatched = detected === 'MACOS' ? 'WINDOWS' : 'MACOS';
    const explicit = loadAppConfig({
      SOURCES: JSON.stringify([
        { type: 'AUTOCHARTIST', url: 'https://example.com/autochartist' },
      ]),
      BROWSER_MODE: 'CDP',
      CDP_AUTO_START: 'false',
      HOST_OS: mismatched,
    });
    expect(() => assertRuntimeConfig(explicit)).toThrow(
      /does not match|unsupported/,
    );
  });

  it('validates CDP URL, booleans, timeout, and loopback detection', () => {
    expect(() =>
      loadAppConfig({ SOURCES: validSources, CDP_ENDPOINT: 'not-a-url' }),
    ).toThrow();
    expect(() =>
      loadAppConfig({ SOURCES: validSources, CDP_AUTO_START: 'sometimes' }),
    ).toThrow();
    expect(() =>
      loadAppConfig({ SOURCES: validSources, CDP_STARTUP_TIMEOUT_MS: '0' }),
    ).toThrow();
    expect(isLoopbackCdpEndpoint('http://127.0.0.1:9222')).toBe(true);
    expect(isLoopbackCdpEndpoint('http://localhost:9222')).toBe(true);
    expect(isLoopbackCdpEndpoint('http://[::1]:9222')).toBe(true);
    expect(isLoopbackCdpEndpoint('https://browser.example.com')).toBe(false);
  });

  it('requires CHROME_EXECUTABLE_PATH to be absolute and existing', () => {
    const autochartistOnly = JSON.stringify([
      { type: 'AUTOCHARTIST', url: 'https://example.com/autochartist' },
    ]);
    const config = loadAppConfig({
      SOURCES: autochartistOnly,
      BROWSER_MODE: 'PERSISTENT',
      CHROME_EXECUTABLE_PATH: './chrome',
    });
    expect(() => assertRuntimeConfig(config)).toThrow(/absolute path/);
    const existing = loadAppConfig({
      SOURCES: autochartistOnly,
      BROWSER_MODE: 'PERSISTENT',
      CHROME_EXECUTABLE_PATH: __filename,
    });
    expect(() => assertRuntimeConfig(existing)).not.toThrow();
  });

  it('parses exact MT5 symbol and per-symbol volume rules', () => {
    expect(
      parseMt5SignalRules(
        JSON.stringify({
          'EUR/USD': { symbol: 'EURUSD.a', volume: '0.10' },
        }),
      ),
    ).toEqual({
      'EUR/USD': { symbol: 'EURUSD.a', volume: '0.10' },
    });
  });

  it.each([
    'not-json',
    JSON.stringify({ 'EUR/USD': { symbol: '', volume: '0.10' } }),
    JSON.stringify({ 'EUR/USD': { symbol: 'EURUSD', volume: '0' } }),
    JSON.stringify({ 'EUR/USD': { symbol: 'EURUSD', volume: 0.1 } }),
  ])('rejects malformed MT5 rules without exposing API keys', (rules) => {
    expect(() =>
      loadAppConfig({ SOURCES: validSources, MT5_SIGNAL_RULES: rules }),
    ).toThrow();
  });

  it('requires an API key and non-empty rules only when MT5 trading is enabled', () => {
    const base = {
      SOURCES: validSources,
      BROWSER_MODE: 'CDP',
      OPENAI_API_KEY: 'openai-key',
      MT5_SIGNAL_TRADING_ENABLED: 'true',
    };
    expect(() => assertRuntimeConfig(loadAppConfig(base))).toThrow(
      /MT5_SIGNAL_API_KEY/,
    );
    expect(() =>
      assertRuntimeConfig(
        loadAppConfig({ ...base, MT5_SIGNAL_API_KEY: 'top-secret-key-value' }),
      ),
    ).toThrow(/MT5_SIGNAL_RULES/);
    const valid = loadAppConfig({
      ...base,
      MT5_SIGNAL_API_KEY: 'top-secret-key-value',
      MT5_SIGNAL_API_URL: 'http://127.0.0.1:8000',
      MT5_SIGNAL_TIMEOUT_MS: '71000',
      MT5_SIGNAL_RULES: JSON.stringify({
        'EUR/USD': { symbol: 'EURUSD', volume: '0.10' },
      }),
    });
    expect(() => assertRuntimeConfig(valid)).not.toThrow();
    expect(valid.MT5_SIGNAL_API_KEY).toBe('top-secret-key-value');
    expect(valid.MT5_SIGNAL_TIMEOUT_MS).toBe(71000);
  });
});
