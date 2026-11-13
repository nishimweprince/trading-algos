"""Tickmind AI signal watcher (headless Playwright, Ubuntu-friendly).

Polls https://tickmindai.com for new / changed signals and notifies.

Two sources, merged:
  * /api/signals          public JSON: id, side, entry/stop/target (closed ones),
                          status, result, postedAtUnix. Active levels come back as 0.
  * the signed-in home page  active signal card with ENTRY / STOP / TARGET.

Every event is appended to state/events.jsonl with our own `seen_utc`, so the
delay between the site's postedAt and when we saw it is measurable.

Env:
  TICKMIND_EMAIL, TICKMIND_PASSWORD   sign-in (only used when the saved session expires).
                                      Unset = API-only mode: new signals, closes and results
                                      are still caught; active entry/stop/target show as 0.
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  optional Telegram delivery
  NTFY_URL                            optional, e.g. https://ntfy.sh/<your-topic>
  POLL_SECONDS                        default 30

Run:  python watch_signals.py            (loop)
      python watch_signals.py --once     (single poll, for cron / testing)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

BASE = "https://tickmindai.com"
STATE = Path(os.environ.get("TICKMIND_STATE_DIR", Path(__file__).parent / "state"))
SESSION = STATE / "session.json"
SEEN = STATE / "seen.json"
EVENTS = STATE / "events.jsonl"
STATUS = STATE / "status.json"   # heartbeat read by ui_server.py
CARD_RE = re.compile(
    r"(?P<pair>[A-Z]{6})\s.*?\b(?P<side>BUY|SELL)\b.*?ENTRY\s*(?P<entry>[\d.,]+)\s*"
    r"STOP\s*(?P<stop>[\d.,]+)\s*TARGET\s*(?P<target>[\d.,]+)", re.S)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def notify(text: str) -> None:
    print(f"[{now()}] {text}", flush=True)
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    try:
        if tok and chat:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=10)
        if os.environ.get("NTFY_URL"):
            urllib.request.urlopen(urllib.request.Request(
                os.environ["NTFY_URL"], data=text.encode(), headers={"Title": "Tickmind"}), timeout=10)
    except Exception as e:  # never let delivery kill the watcher
        print(f"notify failed: {e}", file=sys.stderr)


def have_creds() -> bool:
    return bool(os.environ.get("TICKMIND_EMAIL") and os.environ.get("TICKMIND_PASSWORD"))


def login(page) -> bool:
    """Sign in with env credentials. Returns False (API-only mode) when none are set."""
    if not have_creds():
        return False
    page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    page.wait_for_selector('input[type="email"]', timeout=20_000)
    page.fill('input[type="email"]', os.environ["TICKMIND_EMAIL"])
    page.fill('input[type="password"]', os.environ["TICKMIND_PASSWORD"])
    page.click('form button[type="submit"]')
    page.wait_for_url(lambda u: "/login" not in u, timeout=20_000)
    page.context.storage_state(path=str(SESSION))
    return True


def open_home(page) -> None:
    """Load the home page. The app polls continuously, so wait for content, not network idle."""
    page.goto(BASE + "/", wait_until="domcontentloaded")
    try:
        page.wait_for_selector('article, input[type="email"]', timeout=15_000)
        page.wait_for_timeout(1_500)   # let the signal cards hydrate
    except PWTimeout:
        pass


def active_cards(page) -> tuple[dict[tuple[str, str], dict], bool]:
    """Active signal levels from the signed-in home page, keyed by (pair, side).

    Returns ({}, False) in API-only mode: no saved session and no credentials.
    """
    if not (SESSION.exists() or have_creds()):
        return {}, False
    open_home(page)
    if "/login" in page.url:
        if not login(page):
            return {}, False
        open_home(page)
    out = {}
    for txt in page.locator("article").all_inner_texts():
        m = CARD_RE.search(txt)
        if m:
            d = {k: float(m[k].replace(",", "")) for k in ("entry", "stop", "target")}
            out[(m["pair"], m["side"])] = d
    return out, True


def poll(page, seen: dict) -> tuple[dict, bool]:
    api = page.request.get(f"{BASE}/api/signals").json()["signals"]
    cards, signed_in = active_cards(page)
    for s in api:
        key = s["id"]
        if s["status"] == "active":
            if (s["pair"], s["side"]) in cards:
                s.update(cards[(s["pair"], s["side"])])
            elif not s.get("entry") and seen.get(key, {}).get("entry"):
                # API masks active levels: keep the ones we already have instead of flapping to 0
                s.update({k: seen[key][k] for k in ("entry", "stop", "target")})
        sig = {k: s.get(k) for k in ("pair", "side", "entry", "stop", "target", "status", "result")}
        prev = seen.get(key)
        if prev == sig:
            continue
        kind = "new" if prev is None else "update"
        seen[key] = sig
        lag = time.time() - s["postedAtUnix"]
        with EVENTS.open("a") as f:
            f.write(json.dumps({"seen_utc": now(), "kind": kind, "id": key,
                                "posted_unix": s["postedAtUnix"], "lag_s": round(lag), **sig}) + "\n")
        if kind == "new" and lag > 6 * 3600:
            continue  # first run back-fill: record quietly
        notify(f"{kind.upper()} {sig['pair']} {sig['side']} entry {sig['entry']} "
               f"SL {sig['stop']} TP {sig['target']} [{sig['status']}"
               f"{' ' + str(sig['result']) + ' pips' if sig.get('result') else ''}] lag {lag:.0f}s")
    return seen, signed_in


def heartbeat(ok: bool, err: str | None = None, active: int = 0, signed_in: bool | None = None) -> None:
    prev = json.loads(STATUS.read_text()) if STATUS.exists() else {}
    prev.update({"last_poll_utc": now(), "ok": ok, "active_cards": active})
    if signed_in is not None:
        prev["signed_in"] = signed_in
    if ok:
        prev["last_ok_utc"] = prev["last_poll_utc"]
        prev.pop("last_error", None)
    else:
        prev["last_error"] = (err or "")[:300]
    STATUS.write_text(json.dumps(prev))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    every = int(os.environ.get("POLL_SECONDS", "30"))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=str(SESSION) if SESSION.exists() else None)
        page = ctx.new_page()
        while True:
            try:
                seen, signed_in = poll(page, seen)
                SEEN.write_text(json.dumps(seen))
                if signed_in:
                    ctx.storage_state(path=str(SESSION))
                heartbeat(True, active=sum(v["status"] == "active" for v in seen.values()),
                          signed_in=signed_in)
            except (PWTimeout, Exception) as e:
                print(f"[{now()}] poll error: {e}", file=sys.stderr, flush=True)
                heartbeat(False, str(e))
            if a.once:
                break
            time.sleep(every)
        browser.close()


if __name__ == "__main__":
    main()
