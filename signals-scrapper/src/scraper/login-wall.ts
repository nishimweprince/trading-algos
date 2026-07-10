import { Page } from 'playwright';

/**
 * Heuristics to detect a login wall / expired session before scraping.
 * Returns true if the page looks like the user is NOT authenticated.
 */
export async function isLoginWall(page: Page): Promise<boolean> {
  try {
    const url = page.url().toLowerCase();
    if (
      url.includes('/login') ||
      url.includes('/signin') ||
      url.includes('/auth') ||
      url.includes('sso.')
    ) {
      return true;
    }

    const markers = await page.evaluate(() => {
      const body = document.body?.innerText?.toLowerCase() ?? '';
      const hasPassword = !!document.querySelector(
        'input[type="password"], input[name*="pass" i], input[id*="pass" i]',
      );
      const hasLoginForm = !!document.querySelector(
        'form[action*="login" i], form[id*="login" i], form[class*="login" i]',
      );
      const loginText =
        body.includes('sign in') ||
        body.includes('log in') ||
        body.includes('login to your account') ||
        body.includes('enter your password');
      const loggedInMarker =
        !!document.querySelector(
          '[data-logged-in="true"], .user-menu, .account-menu, #logout, a[href*="logout"]',
        ) ||
        body.includes('logout') ||
        body.includes('log out');
      return { hasPassword, hasLoginForm, loginText, loggedInMarker };
    });

    if (markers.loggedInMarker) return false;
    if (markers.hasPassword && (markers.hasLoginForm || markers.loginText)) {
      return true;
    }
    if (markers.hasLoginForm && markers.loginText) return true;
    return false;
  } catch {
    // If we cannot evaluate, do not treat as login wall — let extraction try.
    return false;
  }
}

/**
 * Pure helper for tests: detect login wall from URL + HTML snapshot.
 */
export function isLoginWallFromSnapshot(opts: {
  url: string;
  html: string;
}): boolean {
  const url = opts.url.toLowerCase();
  if (
    url.includes('/login') ||
    url.includes('/signin') ||
    url.includes('/auth') ||
    url.includes('sso.')
  ) {
    return true;
  }
  const html = opts.html.toLowerCase();
  const hasPassword =
    html.includes('type="password"') || html.includes("type='password'");
  const hasLoginForm =
    html.includes('login') && (html.includes('<form') || html.includes('sign in'));
  const loggedIn =
    html.includes('data-logged-in="true"') ||
    html.includes('logout') ||
    html.includes('log out') ||
    html.includes('user-menu');
  if (loggedIn) return false;
  if (hasPassword && hasLoginForm) return true;
  return false;
}
