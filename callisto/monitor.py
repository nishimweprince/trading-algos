"""Live monitor: watch the channel, classify each new post, emit one payload per NEW trade idea.

Sources:
  web       (default) poll https://t.me/s/<channel> every POLL_SECONDS. No login needed.
  telethon  push updates through a Telegram user session (TELEGRAM_API_ID/HASH). The first
            run asks for your phone number and login code in the terminal; the session file
            (data/callisto.session) is reused afterwards.

Only the EARLIEST post of an idea produces a "signal" payload. Follow-up progress posts of the
same idea are written to the log as "update" events (no alert). Reposted old screenshots
(exact entry price already seen) are logged as "repost" and not alerted.

Outputs: one JSON line per event to stdout and data/events.jsonl; "signal" events are also
POSTed to WEBHOOK_URL and sent to TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID when those are set.

Usage:
  python monitor.py                 # web polling
  python monitor.py --source telethon
  python monitor.py --once          # one poll, then exit (smoke test)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import channel
import classify
import requests
import signals
from config import CFG

STATE = CFG.data_dir / "monitor_state.json"
EVENTS = CFG.data_dir / "events.jsonl"


def payload(idea: signals.Idea, llm: dict, post: dict) -> dict:
    pip = CFG.pip(idea.ticker)
    sgn = 1 if idea.direction == "BUY" else -1
    posted = datetime.fromisoformat(post["time"])
    now = datetime.now(UTC)
    # Reference price for the suggested levels: the latest price shown in the image if any,
    # else the stated entry. The executor should re-anchor SL/TP pips on its actual fill.
    ref = llm.get("current_price") or idea.stated_entry
    call_t = datetime.fromisoformat(idea.image_clock_utc) if idea.image_clock_utc else None
    return {
        "event": "signal",
        "ticker": idea.ticker,
        "direction": idea.direction,
        "entry": idea.stated_entry,
        "entry_range": ([idea.entry_low, idea.entry_high]
                        if idea.entry_low is not None and idea.entry_high is not None else None),
        "reference_price": ref,
        "stop_loss_pips": CFG.sl_pips,
        "take_profit_pips": CFG.tp_pips,
        "stop_loss": round(ref - sgn * CFG.sl_pips * pip, 3) if ref else None,
        "take_profit": round(ref + sgn * CFG.tp_pips * pip, 3) if ref else None,
        "trailing": {"start_pips": CFG.trail_start_pips,
                     "distance_pips": CFG.trail_distance_pips,
                     "step_pips": CFG.trail_step_pips} if CFG.trail_start_pips > 0 else None,
        "channel_sl": idea.stated_sl,
        "channel_tp": idea.stated_tp,
        "age": {
            "pips_in_profit": idea.pips_at_first_sight,         # how far it already ran
            "call_time": call_t.isoformat() if call_t else None,  # from the image clock
            "seconds_since_call": round((posted - call_t).total_seconds()) if call_t else None,
            "seconds_since_post": round((now - posted).total_seconds()),
        },
        "posted_at": post["time"],
        "post_id": post["id"],
        "post_url": f"https://t.me/{CFG.channel}/{post['id']}",
        "confidence": idea.confidence,
        "note": idea.note,
    }


class Monitor:
    def __init__(self):
        self.cache = classify.Cache(CFG.data_dir / "classified.jsonl")
        st = json.loads(STATE.read_text()) if STATE.exists() else {}
        self.last_id = st.get("last_id", 0)
        seen = set(st.get("seen_entries", []))
        if not seen:  # seed from the backtest cache so old screenshots count as reposts
            g = signals.Grouper()
            for r in sorted(self.cache.rows.values(), key=lambda r: r["post"]["id"]):
                g.add(r["post"], r["llm"])
            seen = g.seen_entries
        self.grouper = signals.Grouper(seen)
        for d in st.get("ideas", []):
            self.grouper.ideas.append(signals.Idea(**d))

    def save(self):
        cutoff = (datetime.now(UTC) - timedelta(hours=CFG.group_hours * 2)).isoformat()
        recent = [i.to_dict() for i in self.grouper.ideas if i.last_seen >= cutoff]
        STATE.write_text(json.dumps({"last_id": self.last_id,
                                     "seen_entries": sorted(self.grouper.seen_entries),
                                     "ideas": recent}, indent=1))

    def handle(self, post: channel.Post, images: list[bytes] | None = None):
        row = self.cache.get(post.id)
        if row is None:
            try:
                row = self.cache.put(post, classify.classify_post(post, images))
            except Exception as e:  # noqa: BLE001
                emit({"event": "error", "post_id": post.id, "error": str(e)})
                return
        llm, p = row["llm"], row["post"]
        res = self.grouper.add(p, llm)
        self.last_id = max(self.last_id, post.id)
        if res is None:
            emit({"event": "ignored", "post_id": post.id, "kind": llm.get("kind"),
                  "note": llm.get("note")}, quiet=True)
        else:
            idea, is_new = res
            if not is_new:
                emit({"event": "update", "idea": idea.first_post_id, "post_id": post.id,
                      "pips_in_profit": signals.stated_pips(llm, idea.direction, idea.ticker),
                      "note": llm.get("note")}, quiet=True)
            elif idea.repost:
                emit({"event": "repost", "post_id": post.id, "entry": idea.stated_entry,
                      "note": llm.get("note")})
            elif CFG.max_age_pips and (idea.pips_at_first_sight or 0) > CFG.max_age_pips:
                emit({"event": "too_old", **payload(idea, llm, p)})
            else:
                out = payload(idea, llm, p)
                emit(out)
                notify(out)
        self.save()

    def poll_web(self):
        if self.last_id == 0:  # first start: don't replay history as fresh alerts
            page = channel.fetch_page(CFG.channel)
            self.last_id = max((p.id for p in page), default=0)
            self.save()
            emit({"event": "start", "source": "web", "last_id": self.last_id})
            return
        for post in channel.fetch_new(CFG.channel, self.last_id):
            self.handle(post)


def emit(ev: dict, quiet: bool = False):
    ev = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), **ev}
    line = json.dumps(ev, ensure_ascii=False, default=str)
    with EVENTS.open("a") as f:
        f.write(line + "\n")
    if not quiet:
        print(line, flush=True)


def notify(p: dict):
    if CFG.webhook_url:
        try:
            requests.post(CFG.webhook_url, json=p, timeout=10)
        except requests.RequestException as e:
            print(f"webhook failed: {e}", flush=True)
    if CFG.telegram_bot_token and CFG.telegram_chat_id:
        age = p["age"]
        text = (f"{p['direction']} {p['ticker']}  entry {p['entry'] or '-'}\n"
                f"SL {p['stop_loss']} ({p['stop_loss_pips']:g}p)  "
                f"TP {p['take_profit']} ({p['take_profit_pips']:g}p)\n"
                f"already +{age['pips_in_profit'] if age['pips_in_profit'] is not None else '?'}"
                f" pips, call {age['seconds_since_call'] or '?'}s before post\n{p['post_url']}")
        try:
            requests.post(f"https://api.telegram.org/bot{CFG.telegram_bot_token}/sendMessage",
                          json={"chat_id": CFG.telegram_chat_id, "text": text}, timeout=10)
        except requests.RequestException as e:
            print(f"telegram notify failed: {e}", flush=True)


async def run_telethon(mon: Monitor):
    from telethon import TelegramClient, events

    client = TelegramClient(str(CFG.data_dir / "callisto"), int(CFG.telegram_api_id),
                            CFG.telegram_api_hash)
    await client.start()  # interactive phone/code prompt on first run only
    emit({"event": "start", "source": "telethon"})

    @client.on(events.NewMessage(chats=CFG.channel))
    async def on_msg(ev):
        m = ev.message
        images = []
        if m.photo:
            images.append(await client.download_media(m, file=bytes))
        post = channel.Post(channel=CFG.channel, id=m.id,
                            time=m.date.astimezone(UTC).isoformat(),
                            text=m.message or "", photos=["telethon"] if images else [],
                            reply_to=m.reply_to_msg_id)
        await asyncio.to_thread(mon.handle, post, images or None)

    await client.run_until_disconnected()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["web", "telethon"], default="web")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    mon = Monitor()
    if args.source == "telethon":
        asyncio.run(run_telethon(mon))
        return
    while True:
        try:
            mon.poll_web()
        except Exception as e:  # noqa: BLE001 - keep the watcher alive
            emit({"event": "error", "error": repr(e)})
        if args.once:
            break
        time.sleep(CFG.poll_seconds)


if __name__ == "__main__":
    main()
