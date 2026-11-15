"""Read a public Telegram channel through its web preview (https://t.me/s/<channel>).

No login is needed for public channels. Each page holds ~20 posts; older pages are
fetched with ?before=<post id>. Photos come as CDN URLs that are publicly readable.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"
_session = requests.Session()
_session.headers["User-Agent"] = UA


@dataclass
class Post:
    channel: str
    id: int
    time: str                     # ISO-8601 UTC
    text: str
    photos: list[str] = field(default_factory=list)
    has_video: bool = False
    reply_to: int | None = None   # id of the post this one replies to, if any

    @property
    def ts(self) -> datetime:
        return datetime.fromisoformat(self.time)

    def to_dict(self) -> dict:
        return asdict(self)


def _text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return html.unescape(fragment).strip()


def parse_page(page: str, channel: str) -> list[Post]:
    posts = []
    for block in re.split(r'(?=<div class="tgme_widget_message_wrap)', page)[1:]:
        m = re.search(r'data-post="[^"/]+/(\d+)"', block)
        t = re.search(r'<time datetime="([^"]+)"', block)
        if not m or not t:
            continue
        # The reply quote has its own text div; drop it before reading the post text.
        reply = re.search(r'class="tgme_widget_message_reply"[^>]*href="[^"]*/(\d+)"', block)
        body = re.sub(r'<a class="tgme_widget_message_reply".*?</a>', "", block, flags=re.S)
        txt = re.search(r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>', body, re.S)
        photos = re.findall(
            r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", body
        )
        posts.append(Post(
            channel=channel,
            id=int(m.group(1)),
            time=datetime.fromisoformat(t.group(1)).isoformat(),
            text=_text(txt.group(1)) if txt else "",
            photos=photos,
            has_video="tgme_widget_message_video" in body,
            reply_to=int(reply.group(1)) if reply else None,
        ))
    return posts


def fetch_page(channel: str, before: int | None = None, after: int | None = None) -> list[Post]:
    url = f"https://t.me/s/{channel}"
    params = {}
    if before:
        params["before"] = before
    if after:
        params["after"] = after
    for attempt in range(4):
        r = _session.get(url, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        return parse_page(r.text, channel)
    r.raise_for_status()
    return []


def fetch_since(channel: str, since: datetime, sleep: float = 1.0) -> list[Post]:
    """All posts with time >= since, oldest first."""
    seen: dict[int, Post] = {}
    before = None
    while True:
        page = fetch_page(channel, before=before)
        if not page:
            break
        for p in page:
            seen[p.id] = p
        oldest = min(page, key=lambda p: p.id)
        if oldest.ts < since or (before is not None and oldest.id >= before):
            break
        before = oldest.id
        time.sleep(sleep)
    return sorted((p for p in seen.values() if p.ts >= since), key=lambda p: p.id)


def fetch_new(channel: str, after_id: int) -> list[Post]:
    """Posts newer than after_id, oldest first (one page; called on a short poll)."""
    page = fetch_page(channel) if after_id <= 0 else fetch_page(channel, after=after_id)
    return sorted((p for p in page if p.id > after_id), key=lambda p: p.id)


def download(url: str) -> bytes:
    r = _session.get(url, timeout=30)
    r.raise_for_status()
    return r.content
