#!/usr/bin/env python3
"""pk-predict's performance review from its journal: settled windows, live
fills split into first and pairing legs, and markouts. Standard errors are
clustered by market (one window's fills share one outcome), so a t near 2 is
the bar for calling an edge.

    python3 research/predict_review.py data/state-predict/journal --since 2026-10-27T10:30:00Z
    python3 research/predict_review.py data/state-predict/journal --regime ph0=2026-10-29T00:00Z

Settlements come from `position_settled` and from the `settled` record a
`redeemed` entry carries (windows redeemed before accounting ran live). Fills
are the venue's own `wallet_event` fill rows. --since scores only windows that
opened at or after that time, so one config regime at a time. --regime
NAME=ISO (repeatable) labels each deploy's regime; a window belongs to the
latest regime starting at or before its open, and each regime gets its own
per-window summary line. Markout rows carry no live/shadow flag: pass --since
to leave out a shadow period.
"""
import argparse
import collections
import datetime
import glob
import json
import math
import os
import re
import statistics

KEEP = ('"type":"position_settled"', '"type":"redeemed"', '"type":"markout"', '"type":"wallet_event"', '"type":"fill_tag"')
WINDOW = re.compile(r"-(\d{10})$")
EARLY_MS = 300_000


def unix_ms(iso):
    return datetime.datetime.fromisoformat(iso.rstrip("Z")).replace(tzinfo=datetime.timezone.utc).timestamp() * 1000


def rows(journal):
    files = sorted(glob.glob(os.path.join(journal, "*.jsonl"))) if os.path.isdir(journal) else [journal]
    for path in files:
        with open(path) as f:
            for line in f:
                if any(k in line for k in KEEP):
                    yield json.loads(line)


def window_open_ms(label):
    m = WINDOW.search(label or "")
    return int(m.group(1)) * 1000 if m else None


def clustered(xs):
    """Share-weighted mean ¢/share with its market-clustered standard error, and dollars."""
    q = sum(x["q"] for x in xs)
    mean = sum(x["q"] * x["r"] for x in xs) / q
    by = collections.defaultdict(float)
    for x in xs:
        by[x["market"]] += x["q"] * (x["r"] - mean)
    return 100 * mean, 100 * math.sqrt(sum(v * v for v in by.values())) / q, q * mean


def per_window_line(pnl):
    mean, sd = statistics.mean(pnl), statistics.pstdev(pnl)
    t = mean / (sd / math.sqrt(len(pnl))) if sd > 0 else 0.0
    se = sd / math.sqrt(len(pnl)) if len(pnl) > 1 else 0.0
    return f"per-window mean ${mean:+.3f} ± ${se:.3f}, t {t:+.2f}, n {len(pnl)} (${sum(pnl):+.2f})"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("journal", help="the journal directory (or one .jsonl file)")
    ap.add_argument("--since", help="ISO UTC time; windows opening earlier are skipped")
    ap.add_argument("--regime", action="append", default=[], metavar="NAME=ISO",
                    help="label a regime starting at ISO UTC time (repeatable)")
    args = ap.parse_args()
    since = unix_ms(args.since) if args.since else 0
    regimes = [("all", since)]
    for r in args.regime:
        name, _, iso = r.partition("=")
        if not name or not iso:
            ap.error(f"--regime must be NAME=ISO, got {r!r}")
        regimes.append((name, unix_ms(iso)))
    regimes.sort(key=lambda r: r[1])

    def regime_at(t):
        pick = None
        for i, (name, start) in enumerate(regimes):
            end = regimes[i + 1][1] if i + 1 < len(regimes) else math.inf
            if start <= t < end:
                pick = name
        return pick

    settled, fills, markouts, tags = {}, [], collections.defaultdict(list), []
    for x in rows(args.journal):
        kind = x["type"]
        if kind == "position_settled":
            settled[x["market"]] = x
        elif kind == "redeemed" and isinstance(x.get("settled"), dict):
            settled.setdefault(x["settled"]["market"], x["settled"])
        elif kind == "fill_tag" and x.get("fill_ms", 0) >= since:
            tags.append(x)
        elif kind == "markout" and x["t_ms"] >= since:
            markouts[x["horizon_s"]].append(x)
        elif kind == "wallet_event" and "fill" in x:
            d = x.get("details", {})
            size = int(x["fill"].get("executedSizeWei", "0")) / 1e18
            if size > 0:
                fills.append(dict(t=x["t_ms"], market=str(d.get("marketId")), label=d.get("categorySlug"),
                                  yes=d.get("outcome") == "YES", q=size,
                                  px=int(x["fill"]["executedPriceWei"]) / 1e18))

    outcome = {m: v["outcome"] == "YES" for m, v in settled.items()}
    open_of = {m: (window_open_ms(v.get("label")) or v.get("opened_ms", 0))
               for m, v in settled.items()}
    windows = [v for m, v in settled.items() if open_of[m] >= since]
    for v in windows:
        v["_open"] = window_open_ms(v.get("label")) or v.get("opened_ms", 0)
        v["_regime"] = regime_at(v["_open"])

    net = collections.defaultdict(float)
    scored = []
    for f in sorted(fills, key=lambda f: f["t"]):
        before = net[f["market"]]
        net[f["market"]] += f["q"] if f["yes"] else -f["q"]
        opened = window_open_ms(f["label"]) or open_of.get(f["market"], 0) or f["t"]
        if f["market"] not in outcome or opened < since:
            continue
        opens = before >= -1e-9 if f["yes"] else before <= 1e-9
        scored.append(dict(f, opens=opens, opened=opened, regime=regime_at(opened),
                           r=(1.0 if outcome[f["market"]] == f["yes"] else 0.0) - f["px"]))

    tagged = []
    for t in tags:
        if t["market"] not in outcome:
            continue
        yes = t["side"].upper() == "YES"
        tagged.append(dict(market=t["market"], q=t["quantity"], fill_ms=t["fill_ms"],
                           before=t["spot_before_bps"], pmove=t.get("pmove_c"),
                           after=t.get("spot_after_5s_bps"), regime=regime_at(t["fill_ms"]),
                           r=(1.0 if outcome[t["market"]] == yes else 0.0) - t["price"]))
    # 5 s markouts joined to their fills for the pmove buckets.
    h5 = {(m["market"], m.get("fill_ms")): m["markout"]
          for m in markouts.get(5, []) if m.get("fill_ms") is not None}
    for t in tagged:
        t["m5"] = h5.get((t["market"], t["fill_ms"]))

    names = [n for n, _ in regimes] if len(regimes) > 1 else ["all"]
    for name in names:
        if len(regimes) > 1:
            print(f"== regime {name} ==")
        report_windows([v for v in windows if v["_regime"] == name])
        report_markouts({h: [m for m in ms if regime_at(m["t_ms"]) == name]
                         for h, ms in markouts.items()})
        report_tags([t for t in tagged if t["regime"] == name])
        report_time([f for f in scored if f["regime"] == name])
        report_legs([f for f in scored if f["regime"] == name])


def report_windows(windows):
    print(f"windows settled: {len(windows)}")
    if len(windows) < 2:
        return
    print(per_window_line([v["realised"] for v in windows]))
    two = [v for v in windows if v["yes_qty"] > 1e-9 and v["no_qty"] > 1e-9]
    one = [v for v in windows if v not in two]
    print(f"two-sided {len(two)}: ${sum(v['realised'] for v in two):+.2f}   "
          f"one-sided {len(one)}: ${sum(v['realised'] for v in one):+.2f} ({sum(v['realised'] > 0 for v in one)} won)")
    if one and len(one) >= 2:
        print(f"  one-sided {per_window_line([v['realised'] for v in one])}")
    heavy = [(v["yes_qty"] > v["no_qty"]) == (v["outcome"] == "YES")
             for v in windows if abs(v["yes_qty"] - v["no_qty"]) > 1e-6]
    if heavy:
        print(f"heavier side won {sum(heavy)} of {len(heavy)}")
    for coin in ("btc", "eth", "bnb"):
        xs = [v["realised"] for v in windows if (v.get("label") or "").startswith(coin)]
        if len(xs) >= 2:
            print(f"  {coin.upper():3s} {per_window_line(xs)}")


def report_markouts(markouts):
    for h in sorted(markouts):
        xs = [m["markout"] for m in markouts[h]]
        if len(xs) > 1:
            print(f"markout {h:>3d} s: {100 * statistics.mean(xs):+.2f}¢ ± {100 * statistics.pstdev(xs) / math.sqrt(len(xs)):.2f} (n {len(xs)})")


def report_tags(rows):
    """Fills split by Binance's move and by Predict's own mid move in the
    second before them, both signed so that negative was against the fill."""
    if not rows:
        return
    print("fills by Binance's move in the second before (fill_tag; − = against the fill)")
    for name, sel in (("against (≤ −1 bp)", lambda r: r["before"] <= -1),
                      ("flat", lambda r: -1 < r["before"] < 1),
                      ("with (≥ +1 bp)", lambda r: r["before"] >= 1)):
        xs = [r for r in rows if sel(r)]
        if xs:
            c, se, usd = clustered(xs)
            after = [r["after"] for r in xs if r["after"] is not None]
            print(f"  {name:18s} fills {len(xs):4d}  {c:+6.2f}¢/share ± {se:5.2f}  ${usd:+7.2f}"
                  f"  spot 5 s after {statistics.mean(after) if after else float('nan'):+.1f} bp")
    pm = [r for r in rows if r["pmove"] is not None]
    if pm:
        print("fills by Predict's mid move in the second before (pmove_c; − = against the fill)")
        for name, sel in (("against (≤ −1¢)", lambda r: r["pmove"] <= -1),
                          ("flat", lambda r: -1 < r["pmove"] < 1),
                          ("with (≥ +1¢)", lambda r: r["pmove"] >= 1)):
            xs = [r for r in pm if sel(r)]
            if xs:
                c, se, usd = clustered(xs)
                m5 = [r["m5"] for r in xs if r["m5"] is not None]
                m5s = ""
                if len(m5) > 1:
                    mu, se5 = statistics.mean(m5), statistics.pstdev(m5) / math.sqrt(len(m5))
                    m5s = f"  5 s markout {100 * mu:+.2f}¢ ± {100 * se5:.2f} (n {len(m5)})"
                print(f"  {name:18s} fills {len(xs):4d}  {c:+6.2f}¢/share ± {se:5.2f}  "
                      f"${usd:+7.2f}{m5s}")


def report_time(scored):
    """Fills split by time in window: opening bids go out in the first 5 min."""
    early = [f for f in scored if f["t"] - f["opened"] < EARLY_MS]
    late = [f for f in scored if f["t"] - f["opened"] >= EARLY_MS]
    if not scored:
        return
    print("fills by time in window")
    for name, xs in (("first 5 min", early), ("rest", late)):
        if xs:
            c, se, usd = clustered(xs)
            print(f"  {name:18s} fills {len(xs):4d}  {c:+6.2f}¢/share ± {se:5.2f}  ${usd:+7.2f}")


def report_legs(scored):
    if not scored:
        return
    buckets = [
        ("all", lambda f: True),
        ("price < 0.35", lambda f: f["px"] < 0.35),
        ("price 0.35-0.65", lambda f: 0.35 <= f["px"] < 0.65),
        ("price >= 0.65", lambda f: f["px"] >= 0.65),
        *[(a.upper(), lambda f, a=a: (f["label"] or "").startswith(a)) for a in ("btc", "eth", "bnb")],
    ]
    for leg, pick in (("first legs", lambda f: f["opens"]), ("pairing legs", lambda f: not f["opens"])):
        print(leg)
        for name, sel in buckets:
            xs = [f for f in scored if pick(f) and sel(f)]
            if xs:
                c, se, usd = clustered(xs)
                print(f"  {name:16s} fills {len(xs):4d}  {c:+6.2f}¢/share ± {se:5.2f}  ${usd:+7.2f}")


if __name__ == "__main__":
    main()
