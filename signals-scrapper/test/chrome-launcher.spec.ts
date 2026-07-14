import { ChildProcess, spawn, SpawnOptions } from 'child_process';
import { EventEmitter } from 'events';
import { mkdtempSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import {
  buildChromeArgs,
  chromeExecutableCandidates,
  ChromeLaunchError,
  ChromeLauncherService,
  findChromeExecutable,
} from '../src/browser/chrome-launcher.service';
import { AppConfigService } from '../src/config/app-config.service';

class FakeChild extends EventEmitter {
  pid = 4242;
  exitCode: number | null = null;
  killed = false;
  unref = jest.fn();
  kill = jest.fn();
}

function launcherConfig(dir: string): AppConfigService {
  const config = new AppConfigService();
  config.load({
    SOURCES: JSON.stringify([
      { type: 'TRADING_CENTRAL', url: 'https://example.com/tc' },
      { type: 'TRADING_CENTRAL', url: 'https://example.com/tc' },
    ]),
    BROWSER_MODE: 'CDP',
    HOST_OS: 'MACOS',
    CDP_ENDPOINT: 'http://127.0.0.1:9222',
    USER_DATA_DIR: './profile with spaces',
    CHROME_EXECUTABLE_PATH:
      '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  });
  return config;
}

describe('Chrome launcher', () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'chrome-launcher-'));
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('discovers standard macOS and Windows installations in order', () => {
    expect(chromeExecutableCandidates('MACOS', {}, '/Users/test')).toEqual([
      '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
      '/Users/test/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    ]);
    expect(
      chromeExecutableCandidates(
        'WINDOWS',
        {
          PROGRAMFILES: 'C:\\Program Files',
          'PROGRAMFILES(X86)': 'C:\\Program Files (x86)',
          LOCALAPPDATA: 'C:\\Users\\test\\AppData\\Local',
        },
        'C:\\Users\\test',
      ),
    ).toEqual([
      'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
      'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
      'C:\\Users\\test\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe',
    ]);
  });

  it('uses an existing explicit executable and rejects missing overrides', () => {
    const macPath =
      '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
    expect(findChromeExecutable('MACOS', macPath, (path) => path === macPath)).toBe(
      macPath,
    );
    expect(() =>
      findChromeExecutable('WINDOWS', 'C:\\Custom Chrome\\chrome.exe', () => false),
    ).toThrow(expect.objectContaining<Partial<ChromeLaunchError>>({
      code: 'chrome_executable_not_found',
    }));
  });

  it('builds CDP/profile flags without shell escaping and appends initial URLs', () => {
    expect(
      buildChromeArgs(
        'http://localhost:9222',
        '/tmp/profile with spaces',
        ['https://example.com/tc'],
      ),
    ).toEqual([
      '--remote-debugging-address=127.0.0.1',
      '--remote-debugging-port=9222',
      '--user-data-dir=/tmp/profile with spaces',
      '--no-first-run',
      '--no-default-browser-check',
      'https://example.com/tc',
    ]);
  });

  it('coalesces concurrent launches and leaves the detached process alive', async () => {
    const config = launcherConfig(dir);
    const child = new FakeChild();
    const spawnMock = jest.fn(() => {
      queueMicrotask(() => child.emit('spawn'));
      return child as unknown as ChildProcess;
    });
    const launcher = new ChromeLauncherService(config);
    launcher.setRuntimeForTests({
      platform: 'darwin',
      cwd: dir,
      exists: () => true,
      spawn: spawnMock as unknown as typeof spawn,
    });

    const [first, second] = await Promise.all([
      launcher.ensureRunning(),
      launcher.ensureRunning(),
    ]);
    await launcher.ensureRunning();

    expect(first).toEqual(second);
    expect(spawnMock).toHaveBeenCalledTimes(1);
    const [executable, args, options] = (
      spawnMock.mock.calls as unknown as Array<[
        string,
        string[],
        SpawnOptions,
      ]>
    )[0];
    expect(executable).toContain('Google Chrome');
    expect(args.filter((value: string) => value === 'https://example.com/tc')).toHaveLength(
      1,
    );
    expect(options).toMatchObject({
      detached: true,
      stdio: 'ignore',
      shell: false,
      windowsHide: false,
    });
    expect(child.unref).toHaveBeenCalledTimes(1);
    expect(child.kill).not.toHaveBeenCalled();
    expect(launcher.isProcessAlive()).toBe(true);
  });

  it('surfaces asynchronous spawn errors and retries only after process exit', async () => {
    const config = launcherConfig(dir);
    const failed = new FakeChild();
    const replacement = new FakeChild();
    replacement.pid = 5252;
    const spawnMock = jest
      .fn()
      .mockImplementationOnce(() => {
        queueMicrotask(() => failed.emit('error', new Error('permission denied')));
        return failed as unknown as ChildProcess;
      })
      .mockImplementationOnce(() => {
        queueMicrotask(() => replacement.emit('spawn'));
        return replacement as unknown as ChildProcess;
      });
    const launcher = new ChromeLauncherService(config);
    launcher.setRuntimeForTests({
      platform: 'darwin',
      cwd: dir,
      exists: () => true,
      spawn: spawnMock as unknown as typeof spawn,
    });

    await expect(launcher.ensureRunning()).rejects.toMatchObject({
      code: 'chrome_launch_failed',
    });
    await expect(launcher.ensureRunning()).resolves.toMatchObject({ pid: 5252 });
    expect(spawnMock).toHaveBeenCalledTimes(2);
  });
});
