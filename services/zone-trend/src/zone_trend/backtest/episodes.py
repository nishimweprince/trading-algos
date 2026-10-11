"""Candidate episodes for the golden test set, rendered for hand labelling.

``zt episodes`` replays one symbol and, at every armed retest (signal or
rejection), captures the zones and the breakout time. Each episode is drawn as
an SVG of 1H candles from 48 bars before the breakout to 48 bars after the
signal, with the zones, the breakout bar, entry, stop and targets. A
``labels.yaml`` template lists every episode for you to mark:

    expected_signal: yes | no      # should the bot have taken it?
    zone_ok: yes | no              # is the zone drawn where you would draw it?
    notes: ...

Golden tests (``tests/golden``) replay labelled episodes and fail when the
core's decision disagrees with a ``expected_signal`` you have filled in.
"""

from __future__ import annotations

import bisect
import html
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from zone_trend.config import Config
from zone_trend.core.replay import replay
from zone_trend.data.store import DataPaths, load
from zone_trend.types import Bar, Signal

__all__ = ["Episode", "collect", "write_episodes"]

H = 3_600_000
PAD = 48


@dataclass
class Episode:
    id: str
    symbol: str
    side: str
    bar_time: int
    breakout_time: int
    decision: str  # "signal" | rejection reason
    zones: list[tuple[float, float, str, float]]  # lo, hi, role, strength
    zone: tuple[float, float]
    entry: float | None = None
    stop: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    bars: list[Bar] = field(default_factory=list)


def collect(
    paths: DataPaths, symbol: str, cfg: Config, start: str, end: str, limit: int = 50
) -> list[Episode]:
    lo = int(pd.Timestamp(start + "-01", tz="UTC").value // 1_000_000)
    hi = int((pd.Timestamp(end + "-01", tz="UTC") + pd.offsets.MonthBegin()).value // 1e6)
    entry_tf = cfg.timeframes.entry
    fast_tf, slow_tf = cfg.timeframes.zones[1], cfg.timeframes.zones[0]
    entry, fast, slow = (
        load(paths, "klines", symbol, tf, end_ms=hi + PAD * H)
        for tf in (entry_tf, fast_tf, slow_tf)
    )
    episodes: list[Episode] = []
    history: list[Bar] = []
    for core, step in replay(symbol, entry, fast, slow, cfg):
        history.append(step.bar)
        t = step.bar.close_time
        if not lo <= t < hi or len(episodes) >= limit:
            continue
        found = [(s, "signal") for s in step.result.signals]
        found += [(r, r.reason) for r in step.result.rejected]
        for item, decision in found:
            fsm = core.fsm.get(item.zone_id)
            zone = core.zones.get(item.zone_id)
            ep = Episode(
                id=f"{symbol}-{t}-{item.zone_id}",
                symbol=symbol,
                side=item.side.value,
                bar_time=t,
                breakout_time=fsm.breakout_time if fsm else t,
                decision=decision,
                zones=[(z.lo, z.hi, z.role, z.strength) for z in core.zones.zones],
                zone=(fsm.lo, fsm.hi) if fsm else ((zone.lo, zone.hi) if zone else (0.0, 0.0)),
            )
            if isinstance(item, Signal):
                ep.entry, ep.stop, ep.tp1, ep.tp2 = item.entry_ref, item.stop, item.tp1, item.tp2
            episodes.append(ep)
    # attach the bars around each episode (after-the-fact context is fine for labelling)
    times = [b.close_time for b in history]
    for ep in episodes:
        i0 = max(0, _index(times, ep.breakout_time) - PAD)
        i1 = min(len(history), _index(times, ep.bar_time) + PAD + 1)
        ep.bars = history[i0:i1]
    return episodes


def _index(times: list[int], t: int) -> int:
    return bisect.bisect_left(times, t)


def _svg(ep: Episode, w: int = 960, h: int = 360) -> str:
    bars = ep.bars
    if not bars:
        return ""
    prices = [b.high for b in bars] + [b.low for b in bars]
    prices += [p for p in (ep.stop, ep.tp1, ep.tp2) if p]
    lo_p, hi_p = min(prices), max(prices)
    pad = (hi_p - lo_p) * 0.05 or 1.0
    lo_p, hi_p = lo_p - pad, hi_p + pad
    cw = w / len(bars)

    def y(p: float) -> float:
        return h - (p - lo_p) / (hi_p - lo_p) * h

    parts = []
    for zlo, zhi, role, strength in ep.zones:
        if zhi < lo_p or zlo > hi_p:
            continue
        cls = "sup" if role == "support" else "res"
        parts.append(
            f'<rect class="{cls}" x="0" width="{w}" y="{y(zhi):.1f}" '
            f'height="{max(y(zlo) - y(zhi), 1):.1f}"><title>{role} {strength:.0f}</title></rect>'
        )
    zlo, zhi = ep.zone
    parts.append(
        f'<rect class="hot" x="0" width="{w}" y="{y(zhi):.1f}" '
        f'height="{max(y(zlo) - y(zhi), 1):.1f}"/>'
    )
    for i, b in enumerate(bars):
        x = i * cw + cw / 2
        cls = "up" if b.close >= b.open else "dn"
        if b.close_time in (ep.breakout_time, ep.bar_time):
            parts.append(f'<line class="mark" x1="{x:.1f}" x2="{x:.1f}" y1="0" y2="{h}"/>')
        parts.append(
            f'<line class="{cls}" x1="{x:.1f}" x2="{x:.1f}" '
            f'y1="{y(b.high):.1f}" y2="{y(b.low):.1f}"/>'
        )
        top, bot = y(max(b.open, b.close)), y(min(b.open, b.close))
        parts.append(
            f'<rect class="{cls}" x="{x - cw * 0.35:.1f}" width="{cw * 0.7:.1f}" '
            f'y="{top:.1f}" height="{max(bot - top, 0.8):.1f}"/>'
        )
    for label, p in (("entry", ep.entry), ("stop", ep.stop), ("tp1", ep.tp1), ("tp2", ep.tp2)):
        if p:
            parts.append(f'<line class="{label}" x1="0" x2="{w}" y1="{y(p):.1f}" y2="{y(p):.1f}"/>')
    return f'<svg viewBox="0 0 {w} {h}" width="100%">{"".join(parts)}</svg>'


_CSS = """
:root{--fg:#1d1d1f;--bg:#fff;--muted:#666;--up:#1a7f5a;--dn:#c0392b}
@media (prefers-color-scheme:dark){:root{--fg:#e8e8e8;--bg:#161616;--muted:#999}}
body{font:14px/1.4 system-ui,sans-serif;color:var(--fg);background:var(--bg);max-width:1000px;
margin:2rem auto;padding:0 16px}
.up{stroke:var(--up);fill:var(--up)}.dn{stroke:var(--dn);fill:var(--dn)}
.sup{fill:#1a7f5a22}.res{fill:#c0392b22}.hot{fill:none;stroke:#e6a700;stroke-dasharray:4 3}
.mark{stroke:#8884;stroke-width:1}.entry{stroke:#2e6fd8}.stop{stroke:var(--dn);stroke-dasharray:5 3}
.tp1,.tp2{stroke:var(--up);stroke-dasharray:5 3}.muted{color:var(--muted)}
"""


def write_episodes(episodes: list[Episode], out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    sections = []
    labels = []
    for ep in episodes:
        when = pd.Timestamp(ep.bar_time + 1, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M")
        prices = ""
        if ep.entry:
            prices = f" · entry {ep.entry:.6g} stop {ep.stop:.6g} tp1 {ep.tp1:.6g}"
        sections.append(
            f'<section id="{html.escape(ep.id)}"><h2>{html.escape(ep.symbol)} {ep.side} '
            f"{when} UTC — {html.escape(ep.decision)}</h2>"
            f'<p class="muted">{html.escape(ep.id)}{html.escape(prices)}</p>{_svg(ep)}</section>'
        )
        labels.append(
            {
                "id": ep.id,
                "symbol": ep.symbol,
                "bar_time": ep.bar_time,
                "core_decision": ep.decision,
                "expected_signal": None,
                "zone_ok": None,
                "notes": "",
            }
        )
    page = (
        f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
        f'content="width=device-width,initial-scale=1"><title>Episodes</title>'
        f"<style>{_CSS}</style></head><body><h1>Episodes ({len(episodes)})</h1>"
        f"<p class=muted>Dashed amber: the zone that broke. Vertical lines: breakout and "
        f"signal bars. Fill in labels.yaml next to this page.</p>{''.join(sections)}</body></html>"
    )
    (out / "index.html").write_text(page)
    target = out / "labels.yaml"
    if not target.exists():  # never overwrite labels in progress
        target.write_text(yaml.safe_dump(labels, sort_keys=False))
    return out / "index.html"
