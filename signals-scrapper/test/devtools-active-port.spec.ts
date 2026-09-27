import { join } from 'path';
import {
  defaultChromeUserDataDir,
  parseDevToolsActivePort,
  resolveMainChromeEndpoint,
} from '../src/browser/devtools-active-port';

describe('DevToolsActivePort', () => {
  it('parses port and browser path (LF and CRLF)', () => {
    expect(parseDevToolsActivePort('9222\n/devtools/browser/abc-123\n')).toEqual({
      port: 9222,
      browserPath: '/devtools/browser/abc-123',
    });
    expect(parseDevToolsActivePort('61000\r\n/devtools/browser/x\r\n')).toEqual({
      port: 61000,
      browserPath: '/devtools/browser/x',
    });
  });

  it('rejects malformed contents', () => {
    expect(parseDevToolsActivePort('')).toBeNull();
    expect(parseDevToolsActivePort('abc\n/devtools/browser/x')).toBeNull();
    expect(parseDevToolsActivePort('9222\n/json/version')).toBeNull();
    expect(parseDevToolsActivePort('9222')).toBeNull();
  });

  it('builds the loopback browser WebSocket endpoint', () => {
    const read = jest.fn(() => '9333\n/devtools/browser/uuid\n');
    expect(resolveMainChromeEndpoint('/profile', read)).toBe(
      'ws://127.0.0.1:9333/devtools/browser/uuid',
    );
    expect(read).toHaveBeenCalledWith(join('/profile', 'DevToolsActivePort'));
  });

  it('explains how to enable remote debugging when the file is missing', () => {
    const read = () => {
      throw new Error('ENOENT');
    };
    expect(() => resolveMainChromeEndpoint('/profile', read)).toThrow(
      /chrome:\/\/inspect\/#remote-debugging/,
    );
  });

  it('knows the default main-profile locations', () => {
    expect(defaultChromeUserDataDir('MACOS', {}, '/Users/me')).toBe(
      '/Users/me/Library/Application Support/Google/Chrome',
    );
    expect(
      defaultChromeUserDataDir(
        'WINDOWS',
        { LOCALAPPDATA: 'C:\\Users\\me\\AppData\\Local' },
        'C:\\Users\\me',
      ),
    ).toBe('C:\\Users\\me\\AppData\\Local\\Google\\Chrome\\User Data');
  });
});
