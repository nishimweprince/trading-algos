import { IcLoginService } from '../src/auth/ic-login.service';
import { base32Decode, totp, totpSecondsRemaining } from '../src/auth/totp';
import { AppConfigService } from '../src/config/app-config.service';

describe('TOTP (RFC 6238 SHA-1 test vectors)', () => {
  // RFC 6238 appendix B: ASCII secret "12345678901234567890", 8 digits.
  const secret = Buffer.from('12345678901234567890', 'ascii');
  it.each([
    [59, '94287082'],
    [1111111109, '07081804'],
    [1111111111, '14050471'],
    [1234567890, '89005924'],
    [2000000000, '69279037'],
  ])('t=%i -> %s', (seconds, expected) => {
    expect(totp(secret, seconds * 1000, { digits: 8 })).toBe(expected);
  });

  it('decodes authenticator-style base32 (spaces, lower case)', () => {
    // base32 of "12345678901234567890"
    const b32 = 'gezd gnbv gy3t qojq gezd gnbv gy3t qojq';
    expect(base32Decode(b32).toString('ascii')).toBe('12345678901234567890');
    expect(totp(b32, 59_000)).toBe('287082'); // 6-digit tail of the 8-digit vector
  });

  it('rejects a secret that is not base32', () => {
    expect(() => base32Decode('not-base32!')).toThrow(/base32/);
  });

  it('reports seconds left in the window', () => {
    expect(totpSecondsRemaining(59_000)).toBe(1);
    expect(totpSecondsRemaining(60_000)).toBe(30);
  });
});

describe('IcLoginService guards', () => {
  function service(env: Record<string, string>) {
    const config = new AppConfigService();
    config.load({
      SOURCES: JSON.stringify([{ type: 'TRADING_CENTRAL', url: 'https://example.com/tc' }]),
      ...env,
    });
    return new IcLoginService(config);
  }

  it('does nothing when disabled', async () => {
    const login = service({});
    await expect(login.login({} as never)).resolves.toBe('disabled');
  });

  it('waits out the cooldown between attempts', async () => {
    const login = service({
      AUTO_LOGIN_ENABLED: 'true',
      IC_LOGIN_EMAIL: 'a@b.c',
      IC_LOGIN_PASSWORD: 'pw',
      IC_TOTP_SECRET: 'GEZDGNBVGY3TQOJQ',
      AUTO_LOGIN_COOLDOWN_MINUTES: '30',
    });
    let clock = 1_000_000;
    login.now = () => clock;
    // A page whose login form is not there: the attempt runs and reports it.
    const page = {
      locator: () => ({
        first: () => ({ isVisible: async () => false }),
      }),
    };
    await expect(login.login(page as never)).resolves.toBe('no_login_form');
    clock += 10 * 60_000;
    await expect(login.login(page as never)).resolves.toBe('cooldown');
    clock += 25 * 60_000;
    await expect(login.login(page as never)).resolves.toBe('no_login_form');
  });
});
