import { Injectable, Logger } from '@nestjs/common';
import { Page } from 'playwright';
import { AppConfigService } from '../config/app-config.service';
import { totp, totpSecondsRemaining } from './totp';

export type LoginOutcome =
  | 'logged_in'
  | 'disabled'
  | 'cooldown'
  | 'no_login_form'
  | 'captcha'
  | 'rejected'
  | 'two_factor_failed'
  | 'error';

/**
 * Signs the scraper's Chrome back into the IC Markets client area (secure.ic.com) when the
 * session has expired: cookie banner -> email/password -> TOTP code from IC_TOTP_SECRET.
 *
 * It is an ordinary browser login. The page runs reCAPTCHA Enterprise; nothing here tries
 * to evade or solve it. When IC Markets asks for a challenge, the attempt stops with
 * `captcha` and the operator signs in once by hand (see README, "Manual login").
 * A cooldown stops repeated failures from locking the account.
 */
@Injectable()
export class IcLoginService {
  private readonly logger = new Logger(IcLoginService.name);
  private lastAttemptMs = 0;
  /** Test hook for the clock. */
  now: () => number = () => Date.now();

  constructor(private readonly config: AppConfigService) {}

  async login(page: Page): Promise<LoginOutcome> {
    if (!this.config.autoLoginEnabled) return 'disabled';
    const cooldownMs = this.config.autoLoginCooldownMinutes * 60_000;
    if (this.lastAttemptMs && this.now() - this.lastAttemptMs < cooldownMs) {
      return 'cooldown';
    }
    this.lastAttemptMs = this.now();
    try {
      const outcome = await this.attempt(page);
      const level = outcome === 'logged_in' ? 'log' : 'warn';
      this.logger[level](`IC Markets auto-login: ${outcome}`);
      return outcome;
    } catch (err) {
      this.logger.warn(
        `IC Markets auto-login failed: ${err instanceof Error ? err.message : String(err)}`,
      );
      return 'error';
    }
  }

  private async attempt(page: Page): Promise<LoginOutcome> {
    const waitMs = Math.max(this.config.navTimeoutMs, 45_000);
    await this.dismissCookieBanner(page);

    // A previous attempt may have left the tab on the 2FA screen: go straight to the code.
    if ((await this.twoFactorInputs(page)).length === 0) {
      const email = page.locator('input#email, input[type="email"]').first();
      const password = page.locator('input#password, input[type="password"]').first();
      if (!(await email.isVisible().catch(() => false))) return 'no_login_form';

      await email.fill(this.config.icLoginEmail);
      await password.fill(this.config.icLoginPassword);
      await page
        .locator('button[type="submit"]', { hasText: /log\s*in|sign\s*in/i })
        .first()
        .click();
      // The login posts in the background (reCAPTCHA token + XHR) and the button spins;
      // "network idle" fires before it settles, so wait for an actual outcome.
      const step = await this.waitForOutcome(page, waitMs, 'login');
      if (step === 'captcha') return 'captcha';
      if (step === 'error') return 'rejected';
      if (step === 'left_login') return 'logged_in'; // no second factor (remembered device)
      if (step === 'timeout') return (await this.stillOnLoginForm(page)) ? 'rejected' : 'error';
    }

    const codeInputs = await this.twoFactorInputs(page);
    if (codeInputs.length === 0) return 'error';

    // A code about to expire may be stale by the time it is checked; wait for a fresh one.
    if (totpSecondsRemaining(this.now()) < 5) {
      await page.waitForTimeout(totpSecondsRemaining(this.now()) * 1000 + 500);
    }
    const code = totp(this.config.icTotpSecret, this.now());
    if (codeInputs.length >= 6) {
      for (let i = 0; i < 6; i += 1) await codeInputs[i].fill(code[i]);
    } else {
      await codeInputs[0].fill(code);
    }
    const verify = page.locator('#VerifyBtn').first();
    const confirm = (await verify.isVisible().catch(() => false))
      ? verify
      : page
          .locator('button[type="submit"], input[type="submit"], button', {
            hasText: /verify|confirm|continue|submit|log\s*in|sign\s*in/i,
          })
          .first();
    if (await confirm.isVisible().catch(() => false)) await confirm.click();
    else await codeInputs[codeInputs.length >= 6 ? 5 : 0].press('Enter');

    const step = await this.waitForOutcome(page, waitMs, 'two_factor');
    if (step === 'captcha') return 'captcha';
    if (step === 'left_two_factor' || step === 'left_login') return 'logged_in';
    return 'two_factor_failed';
  }

  /**
   * Poll until the submit has a visible result. For the login step: the 2FA field shows,
   * the page leaves /Account/LogOn, an error appears, or a challenge appears. For the 2FA
   * step: the page leaves /TwoFactor (or an error/challenge appears).
   */
  private async waitForOutcome(
    page: Page,
    timeoutMs: number,
    stage: 'login' | 'two_factor',
  ): Promise<
    'two_factor' | 'left_login' | 'left_two_factor' | 'captcha' | 'error' | 'timeout'
  > {
    const end = Date.now() + timeoutMs;
    while (Date.now() < end) {
      await page.waitForTimeout(500);
      if (await this.captchaChallengeVisible(page)) return 'captcha';
      const path = (() => {
        try {
          return new URL(page.url()).pathname.toLowerCase();
        } catch {
          return '';
        }
      })();
      if (stage === 'login') {
        if ((await this.twoFactorInputs(page)).length > 0) return 'two_factor';
        if (path && !path.includes('/account/logon')) {
          if (path.includes('/twofactor')) continue; // 2FA page still rendering
          return 'left_login';
        }
      } else if (path && !path.includes('/twofactor') && !path.includes('/account/logon')) {
        return 'left_two_factor';
      }
      if (await this.errorShown(page)) return 'error';
    }
    return 'timeout';
  }

  private async errorShown(page: Page): Promise<boolean> {
    return page
      .locator('.validation-summary-errors, .field-validation-error, [role="alert"], .alert-danger, .text-danger')
      .filter({ hasText: /\S/ })
      .first()
      .isVisible()
      .catch(() => false);
  }

  private async dismissCookieBanner(page: Page): Promise<void> {
    // Usercentrics banner: decline non-essential cookies.
    const deny = page.locator('button#deny, button:has-text("Deny all")').first();
    if (await deny.isVisible().catch(() => false)) {
      await deny.click().catch(() => undefined);
    }
  }

  private async twoFactorInputs(page: Page) {
    const selectors = [
      'input#inpTwoFactorKey',
      'input[name="TwoFactorKey"]',
      'input[autocomplete="one-time-code"]',
      'input[name*="code" i]:not([type="hidden"])',
      'input[id*="code" i]:not([type="hidden"])',
      'input[name*="otp" i]',
      'input[id*="otp" i]',
      'input[name*="token" i]:not([type="hidden"])',
      'input[maxlength="1"][inputmode="numeric"]',
      'input[maxlength="6"]',
    ];
    const found = page.locator(selectors.join(', '));
    const all = await found.all();
    const visible = [];
    for (const input of all) {
      if (await input.isVisible().catch(() => false)) visible.push(input);
    }
    return visible;
  }

  private async stillOnLoginForm(page: Page): Promise<boolean> {
    return page
      .locator('input#password, input[type="password"]')
      .first()
      .isVisible()
      .catch(() => false);
  }

  /** A visible reCAPTCHA challenge frame (the image puzzle), not the invisible badge. */
  private async captchaChallengeVisible(page: Page): Promise<boolean> {
    for (const frame of page.frames()) {
      if (!/recaptcha\/(enterprise|api2)\/bframe/.test(frame.url())) continue;
      const element = await frame.frameElement().catch(() => null);
      if (element && (await element.isVisible().catch(() => false))) return true;
    }
    return false;
  }
}
