import { readFileSync } from 'fs';
import { homedir } from 'os';
import { join, win32 } from 'path';
import { ResolvedHostOs } from '../config/sources.schema';

/** File Chrome writes into its user-data dir while remote debugging is enabled. */
export const DEVTOOLS_ACTIVE_PORT_FILE = 'DevToolsActivePort';

export const ENABLE_REMOTE_DEBUGGING_HINT =
  'Open chrome://inspect/#remote-debugging in your main Chrome, enable "Allow remote debugging for this browser instance", then click "Allow" when Chrome asks to let signals-scrapper connect.';

/** Default user-data dir of the everyday (main) Chrome install. */
export function defaultChromeUserDataDir(
  os: ResolvedHostOs,
  env: NodeJS.ProcessEnv = process.env,
  home: string = homedir(),
): string {
  if (os === 'MACOS') {
    return join(home, 'Library', 'Application Support', 'Google', 'Chrome');
  }
  const localAppData =
    env.LOCALAPPDATA ??
    env.LocalAppData ??
    win32.join(home, 'AppData', 'Local');
  return win32.join(localAppData, 'Google', 'Chrome', 'User Data');
}

export interface DevToolsActivePort {
  port: number;
  browserPath: string;
}

/** Parse `<port>\n/devtools/browser/<id>`; returns null when malformed. */
export function parseDevToolsActivePort(contents: string): DevToolsActivePort | null {
  const [portLine, pathLine] = contents.split(/\r?\n/).map((line) => line.trim());
  const port = Number(portLine);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) return null;
  if (!pathLine || !pathLine.startsWith('/devtools/browser/')) return null;
  return { port, browserPath: pathLine };
}

/**
 * Resolve the browser-level WebSocket endpoint of the already-running main
 * Chrome. Throws with an actionable message when debugging is not enabled.
 */
export function resolveMainChromeEndpoint(
  userDataDir: string,
  readFile: (path: string) => string = (path) => readFileSync(path, 'utf8'),
  pathJoin: (...parts: string[]) => string = join,
): string {
  const file = pathJoin(userDataDir, DEVTOOLS_ACTIVE_PORT_FILE);
  let contents: string;
  try {
    contents = readFile(file);
  } catch {
    throw new Error(
      `Main Chrome is not accepting debugging connections (${file} not found). ${ENABLE_REMOTE_DEBUGGING_HINT}`,
    );
  }
  const parsed = parseDevToolsActivePort(contents);
  if (!parsed) {
    throw new Error(
      `Main Chrome ${file} is malformed. Restart Chrome, then: ${ENABLE_REMOTE_DEBUGGING_HINT}`,
    );
  }
  return `ws://127.0.0.1:${parsed.port}${parsed.browserPath}`;
}
