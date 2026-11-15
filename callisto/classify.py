"""Ask OpenAI whether a channel post is a trade signal and pull out its fields.

Results are cached in data/classified.jsonl by post id, so re-running the backtest
never pays for the same post twice.
"""
from __future__ import annotations

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import channel
from config import CFG
from openai import OpenAI

PROMPT = """You read posts from a forex/gold Telegram signal channel (CallistoFX) and decide
whether each post is a TRADE SIGNAL a follower could still act on.

The channel posts mostly marketing. Real trade information usually comes as IMAGES:
- a screenshot of a "CallistoFx Premium Channel" message such as "XAUUSD BUY RANGE: 4121-4115"
  (SL/TP are often blurred) with a clock time in the corner, e.g. "1:55 pm";
- a MetaTrader position screenshot, e.g. "XAUUSD.s, buy 24.00  4120.39 -> 4125.17   11 472.00"
  meaning a BUY opened at 4120.39, current price 4125.17, profit 11,472 (account currency);
  several rows can show the same trade scaled in at nearby prices;
- text like "Still IN 140 pips", "+60 pips running", "Executing LIVE".

kind (pick one):
- "signal": a call or an OPEN position that is still running (entry range, live position
  screenshot, "still in", "running", "executing now"). Progress updates on an open trade are
  also "signal".
- "closed_result": a trade already closed / recap / trade log / "FULL TP" / "SIGNAL WIN" posted
  after the fact.
- "member_result": profits of members/clients ("X copied our trades", P&L of followers).
- "analysis": zones/levels/outlook without an entry being taken ("BUY ZONE 4054-4027 once
  price taps...").
- "promo": marketing, livestream adverts, join links, motivation.
- "other": anything else.

Fields (use null when not shown):
- ticker: normalised symbol, e.g. "XAUUSD" (strip broker suffixes like ".s"; gold = XAUUSD).
- direction: "BUY" or "SELL".
- entry_low / entry_high: entry range if given (for a single entry price set both equal).
- entry_price: the position open price; average if several rows of the same trade.
- current_price: the current/close price shown in a position screenshot.
- pips_in_profit: pips already in profit as stated in text or image. If not stated but entry
  and current price are shown, leave null (it is computed later).
- stop_loss / take_profit: price levels if readable.
- image_clock: a clock time printed inside the image (e.g. the Telegram time "1:55 pm") as
  24h "HH:MM", else null.
- confidence: 0..1 that kind and direction are right.
- note: one short sentence on what you saw.
Only the CHANNEL's own trade counts as "signal"; member/client screenshots are "member_result".
"""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": [
            "signal", "closed_result", "member_result", "analysis", "promo", "other"]},
        "ticker": {"type": ["string", "null"]},
        "direction": {"type": ["string", "null"], "enum": ["BUY", "SELL", None]},
        "entry_low": {"type": ["number", "null"]},
        "entry_high": {"type": ["number", "null"]},
        "entry_price": {"type": ["number", "null"]},
        "current_price": {"type": ["number", "null"]},
        "pips_in_profit": {"type": ["number", "null"]},
        "stop_loss": {"type": ["number", "null"]},
        "take_profit": {"type": ["number", "null"]},
        "image_clock": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "note": {"type": "string"},
    },
    "required": ["kind", "ticker", "direction", "entry_low", "entry_high", "entry_price",
                 "current_price", "pips_in_profit", "stop_loss", "take_profit",
                 "image_clock", "confidence", "note"],
}

_client: OpenAI | None = None
_lock = threading.Lock()


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=CFG.openai_api_key)
    return _client


def classify_post(post: channel.Post, images: list[bytes] | None = None) -> dict:
    """images: raw image bytes (Telethon source); otherwise post.photos URLs are fetched."""
    if not post.text and not post.photos and not images:
        return {"kind": "other", "note": "empty post (video/sticker only)", "skipped": True}
    content = [{"type": "input_text",
                "text": f"Post time (UTC): {post.time}\nPost text:\n{post.text or '(none)'}"}]
    blobs: list[bytes | Exception] = list(images or [])[:CFG.max_images]
    for url in ([] if images else post.photos[:CFG.max_images]):
        try:
            blobs.append(channel.download(url))
        except Exception as e:  # noqa: BLE001 - one bad image should not drop the post
            blobs.append(e)
    for blob in blobs:
        if isinstance(blob, Exception):
            content.append({"type": "input_text", "text": f"(image failed to load: {blob})"})
            continue
        b64 = base64.b64encode(blob).decode()
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}",
                        "detail": "high"})
    kwargs = {}
    if CFG.openai_model.startswith(("gpt-5", "o")):
        kwargs["reasoning"] = {"effort": CFG.openai_reasoning}
    resp = client().responses.create(
        model=CFG.openai_model,
        store=False,
        instructions=PROMPT,
        input=[{"role": "user", "content": content}],
        text={"format": {"type": "json_schema", "name": "post_signal", "schema": SCHEMA,
                         "strict": True}},
        **kwargs,
    )
    out = json.loads(resp.output_text)
    out["model"] = CFG.openai_model
    return out


class Cache:
    def __init__(self, path: Path):
        self.path = path
        self.rows: dict[int, dict] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    r = json.loads(line)
                    self.rows[r["post"]["id"]] = r

    def get(self, post_id: int) -> dict | None:
        return self.rows.get(post_id)

    def put(self, post: channel.Post, result: dict) -> dict:
        row = {"post": post.to_dict(), "llm": result}
        with _lock:
            self.rows[post.id] = row
            with self.path.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row


def classify_all(posts: list[channel.Post], cache: Cache, workers: int = 8) -> list[dict]:
    todo = [p for p in posts if cache.get(p.id) is None]

    def work(p):
        try:
            return cache.put(p, classify_post(p))
        except Exception as e:  # noqa: BLE001 - keep going; failure is not cached
            print(f"  post {p.id}: classify failed: {e}")
            return None

    if todo:
        print(f"classifying {len(todo)} posts with {CFG.openai_model} ...")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for n, _ in enumerate(ex.map(work, todo), 1):
                if n % 50 == 0:
                    print(f"  {n}/{len(todo)}")
    return [cache.get(p.id) for p in posts if cache.get(p.id) is not None]


if __name__ == "__main__":
    import sys
    raw = json.load(open(sys.argv[1]))
    ids = {int(x) for x in sys.argv[2:]}
    for d in raw:
        if d["id"] in ids:
            print(d["id"], json.dumps(classify_post(channel.Post(**d)), indent=1))

