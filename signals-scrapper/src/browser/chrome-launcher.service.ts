import { Injectable, Logger } from '@nestjs/common';
import { ChildProcess, spawn } from 'child_process';
import { existsSync, mkdirSync } from 'fs';
import { homedir } from 'os';
import { isAbsolute, join, resolve, win32 } from 'path';
import { AppConfigService } from '../config/app-config.service';
import {
  ResolvedHostOs,
  resolveHostOs,
} from '../config/sources.schema';

export type ChromeLaunchErrorCode =
  | 'unsupported_host_os'
  | 'chrome_executable_not_found'
  | 'chrome_launch_failed';

export class ChromeLaunchError extends Error {
  constructor(
    public readonly code: ChromeLaunchErrorCode,
    message: string,
  ) {
    super(message);
    this.name = 'ChromeLaunchError';
  }
}

export interface ChromeLaunchResult {
  executablePath: string;
  userDataDir: string;
  pid?: number;
}

type SpawnFunction = typeof spawn;
type ExistsFunction = (path: string) => boolean;

export function chromeExecutableCandidates(
  os: ResolvedHostOs,
  env: NodeJS.ProcessEnv = process.env,
  home: string = homedir(),
): string[] {
  if (os === 'MACOS') {
    return [
      '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
      join(home, 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
    ];
  }

  const programFiles = env.PROGRAMFILES ?? env.ProgramFiles;
  const programFilesX86 =
    env['PROGRAMFILES(X86)'] ?? env['ProgramFiles(x86)'];
  const localAppData = env.LOCALAPPDATA ?? env.LocalAppData;
  return [
    programFiles
      ? win32.join(programFiles, 'Google', 'Chrome', 'Application', 'chrome.exe')
      : undefined,
    programFilesX86
      ? win32.join(programFilesX86, 'Google', 'Chrome', 'Application', 'chrome.exe')
      : undefined,
    localAppData
      ? win32.join(localAppData, 'Google', 'Chrome', 'Application', 'chrome.exe')
      : undefined,
  ].filter((value): value is string => !!value);
}

export function findChromeExecutable(
  os: ResolvedHostOs,
  override: string,
  exists: ExistsFunction = existsSync,
  env: NodeJS.ProcessEnv = process.env,
  home: string = homedir(),
): string {
  if (override.trim()) {
    const pathApi = os === 'WINDOWS' ? win32 : { isAbsolute };
    if (!pathApi.isAbsolute(override) || !exists(override)) {
      throw new ChromeLaunchError(
        'chrome_executable_not_found',
        'CHROME_EXECUTABLE_PATH must be an existing absolute file.',
      );
    }
    return override;
  }
  const found = chromeExecutableCandidates(os, env, home).find(exists);
  if (!found) {
    throw new ChromeLaunchError(
      'chrome_executable_not_found',
      `Google Chrome was not found in the standard ${os} installation locations; set CHROME_EXECUTABLE_PATH.`,
    );
  }
  return found;
}

export function buildChromeArgs(
  endpoint: string,
  userDataDir: string,
  initialUrls: string[],
): string[] {
  const url = new URL(endpoint);
  const port = url.port || (url.protocol === 'https:' ? '443' : '80');
  const hostname = url.hostname.replace(/^\[|\]$/g, '');
  const address = hostname === 'localhost' ? '127.0.0.1' : hostname;
  return [
    `--remote-debugging-address=${address}`,
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${userDataDir}`,
    '--no-first-run',
    '--no-default-browser-check',
    ...initialUrls,
  ];
}

@Injectable()
export class ChromeLauncherService {
  private readonly logger = new Logger(ChromeLauncherService.name);
  private child: ChildProcess | null = null;
  private launchPromise: Promise<ChromeLaunchResult> | null = null;
  private lastResult: ChromeLaunchResult | null = null;
  private spawnFunction: SpawnFunction = spawn;
  private existsFunction: ExistsFunction = existsSync;
  private runtimePlatform: NodeJS.Platform = process.platform;
  private runtimeEnv: NodeJS.ProcessEnv = process.env;
  private runtimeHome: string = homedir();
  private runtimeCwd: string = process.cwd();

  constructor(private readonly config: AppConfigService) {}

  async ensureRunning(): Promise<ChromeLaunchResult> {
    if (this.isProcessAlive() && this.lastResult) return this.lastResult;
    if (this.launchPromise) return this.launchPromise;
    this.launchPromise = this.launch().finally(() => {
      this.launchPromise = null;
    });
    return this.launchPromise;
  }

  isProcessAlive(): boolean {
    return !!this.child && this.child.exitCode === null && !this.child.killed;
  }

  /** Test-only runtime injection; production uses Node process and filesystem APIs. */
  setRuntimeForTests(runtime: {
    platform?: NodeJS.Platform;
    env?: NodeJS.ProcessEnv;
    home?: string;
    cwd?: string;
    exists?: ExistsFunction;
    spawn?: SpawnFunction;
  }): void {
    this.runtimePlatform = runtime.platform ?? this.runtimePlatform;
    this.runtimeEnv = runtime.env ?? this.runtimeEnv;
    this.runtimeHome = runtime.home ?? this.runtimeHome;
    this.runtimeCwd = runtime.cwd ?? this.runtimeCwd;
    this.existsFunction = runtime.exists ?? this.existsFunction;
    this.spawnFunction = runtime.spawn ?? this.spawnFunction;
  }

  private async launch(): Promise<ChromeLaunchResult> {
    let os: ResolvedHostOs;
    try {
      os = resolveHostOs(this.config.hostOs, this.runtimePlatform);
    } catch (err) {
      throw new ChromeLaunchError(
        'unsupported_host_os',
        err instanceof Error ? err.message : String(err),
      );
    }
    const executablePath = findChromeExecutable(
      os,
      this.config.chromeExecutablePath,
      this.existsFunction,
      this.runtimeEnv,
      this.runtimeHome,
    );
    const userDataDir = resolve(this.runtimeCwd, this.config.userDataDir);
    mkdirSync(userDataDir, { recursive: true });
    const initialUrls = [
      ...new Set(
        this.config.sources
          .filter((source) => source.type === 'TRADING_CENTRAL')
          .map((source) => source.url),
      ),
    ];
    const args = buildChromeArgs(
      this.config.cdpEndpoint,
      userDataDir,
      initialUrls,
    );

    this.logger.log(
      `Starting Chrome for ${os}: executable=${executablePath}, profile=${userDataDir}`,
    );
    let child: ChildProcess;
    try {
      child = this.spawnFunction(executablePath, args, {
        detached: true,
        stdio: 'ignore',
        shell: false,
        windowsHide: false,
      });
    } catch (err) {
      throw new ChromeLaunchError(
        'chrome_launch_failed',
        `Chrome could not be started: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
    this.child = child;

    return new Promise<ChromeLaunchResult>((resolveLaunch, rejectLaunch) => {
      const onError = (err: Error): void => {
        if (this.child === child) this.child = null;
        rejectLaunch(
          new ChromeLaunchError(
            'chrome_launch_failed',
            `Chrome could not be started: ${err.message}`,
          ),
        );
      };
      child.once('error', onError);
      child.once('spawn', () => {
        child.removeListener('error', onError);
        child.on('error', (err) => {
          this.logger.warn(`Chrome process error: ${err.message}`);
        });
        child.unref();
        const result: ChromeLaunchResult = {
          executablePath,
          userDataDir,
          pid: child.pid,
        };
        this.lastResult = result;
        this.logger.log(`Chrome process started${child.pid ? ` (pid=${child.pid})` : ''}`);
        resolveLaunch(result);
      });
      child.once('exit', (code, signal) => {
        if (this.child === child) this.child = null;
        this.logger.warn(
          `Chrome launch process exited before scraper shutdown (code=${String(code)}, signal=${String(signal)})`,
        );
      });
    });
  }
}
