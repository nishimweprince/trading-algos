import { Browser, BrowserContext } from 'playwright';
import {
  BrowserAccessError,
  BrowserService,
} from '../src/browser/browser.service';
import { ChromeLauncherService } from '../src/browser/chrome-launcher.service';
import { AppConfigService } from '../src/config/app-config.service';

function config(
  overrides: Record<string, string> = {},
): AppConfigService {
  const value = new AppConfigService();
  value.load({
    SOURCES: JSON.stringify([
      { type: 'TRADING_CENTRAL', url: 'https://example.com/tc' },
    ]),
    BROWSER_MODE: 'CDP',
    CDP_ENDPOINT: 'http://127.0.0.1:9222',
    CDP_AUTO_START: 'true',
    CDP_STARTUP_TIMEOUT_MS: '5',
    ...overrides,
  });
  return value;
}

function fakeBrowser(): { browser: Browser; context: BrowserContext } {
  const context = { pages: () => [] } as unknown as BrowserContext;
  const browser = {
    contexts: () => [context],
    close: jest.fn().mockResolvedValue(undefined),
  } as unknown as Browser;
  return { browser, context };
}

function fakeLauncher(alive = true): {
  launcher: ChromeLauncherService;
  ensureRunning: jest.Mock;
  isProcessAlive: jest.Mock;
} {
  const ensureRunning = jest.fn().mockResolvedValue({
    executablePath: '/chrome',
    userDataDir: '/profile',
    pid: 1,
  });
  const isProcessAlive = jest.fn(() => alive);
  return {
    launcher: {
      ensureRunning,
      isProcessAlive,
    } as unknown as ChromeLauncherService,
    ensureRunning,
    isProcessAlive,
  };
}

describe('BrowserService CDP auto-start recovery', () => {
  it('attaches immediately without launching when CDP is already healthy', async () => {
    const connected = fakeBrowser();
    const launch = fakeLauncher();
    const service = new BrowserService(config(), launch.launcher);
    const connector = jest.fn().mockResolvedValue(connected.browser);
    service.setCdpRuntimeForTests(connector);

    await expect(service.ensureContext()).resolves.toBe(connected.context);

    expect(connector).toHaveBeenCalledTimes(1);
    expect(launch.ensureRunning).not.toHaveBeenCalled();
  });

  it('launches once after failure, polls, and attaches to the new Chrome', async () => {
    const connected = fakeBrowser();
    const launch = fakeLauncher();
    const service = new BrowserService(config(), launch.launcher);
    const connector = jest
      .fn()
      .mockRejectedValueOnce(new Error('ECONNREFUSED'))
      .mockResolvedValueOnce(connected.browser);
    service.setCdpRuntimeForTests(connector);

    await expect(service.ensureContext()).resolves.toBe(connected.context);

    expect(launch.ensureRunning).toHaveBeenCalledTimes(1);
    expect(connector).toHaveBeenCalledTimes(2);
  });

  it('does not auto-start for a remote CDP endpoint', async () => {
    const launch = fakeLauncher();
    const service = new BrowserService(
      config({ CDP_ENDPOINT: 'https://browser.example.com:9222' }),
      launch.launcher,
    );
    service.setCdpRuntimeForTests(
      jest.fn().mockRejectedValue(new Error('unavailable')),
    );

    await expect(service.ensureContext()).rejects.toMatchObject<
      Partial<BrowserAccessError>
    >({ code: 'cdp_unavailable' });
    expect(launch.ensureRunning).not.toHaveBeenCalled();
  });

  it('respects CDP_AUTO_START=false for local endpoints', async () => {
    const launch = fakeLauncher();
    const service = new BrowserService(
      config({ CDP_AUTO_START: 'false' }),
      launch.launcher,
    );
    service.setCdpRuntimeForTests(
      jest.fn().mockRejectedValue(new Error('unavailable')),
    );

    await expect(service.ensureContext()).rejects.toMatchObject({
      code: 'cdp_unavailable',
    });
    expect(launch.ensureRunning).not.toHaveBeenCalled();
  });

  it('returns cdp_start_timeout while a launched process stays alive', async () => {
    const launch = fakeLauncher(true);
    const service = new BrowserService(
      config({ CDP_STARTUP_TIMEOUT_MS: '2' }),
      launch.launcher,
    );
    service.setCdpRuntimeForTests(
      jest.fn().mockRejectedValue(new Error('still unavailable')),
      1,
    );

    await expect(service.ensureContext()).rejects.toMatchObject({
      code: 'cdp_start_timeout',
    });
    expect(launch.ensureRunning).toHaveBeenCalledTimes(1);
  });

  it('returns chrome_launch_failed when the launched process exits early', async () => {
    const launch = fakeLauncher(false);
    const service = new BrowserService(config(), launch.launcher);
    service.setCdpRuntimeForTests(
      jest.fn().mockRejectedValue(new Error('still unavailable')),
    );

    await expect(service.ensureContext()).rejects.toMatchObject({
      code: 'chrome_launch_failed',
    });
    expect(launch.ensureRunning).toHaveBeenCalledTimes(1);
  });
});
