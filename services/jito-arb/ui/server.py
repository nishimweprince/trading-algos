#!/usr/bin/env python3
"""jito-arb dashboard: a read-only view of the running bot (Python 3.9 stdlib only).

It samples the bot's Prometheus endpoint every 5 s into a 6-hour ring buffer, reads the decision logs
(src/obs/decision_log.hpp layout) and the discovery pools file, and serves one page plus JSON:

  /                 the dashboard (index.html next to this file)
  /api/state        latest metrics, rates, service states, discovery summary
  /api/history      sampled series for the charts (?minutes=60)
  /api/decisions    decision-log summary and the latest records (?hours=24)

  python3 server.py [--bind 127.0.0.1] [--port 8088] [--metrics URL] [--data /var/lib/jito-arb]

It never writes anything and exposes no secrets. Bind to localhost and publish it through a tunnel
with access control in front (it has no login of its own).
"""
import argparse
import collections
import datetime
import glob
import json
import os
import struct
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_S = 5
KEEP_S = 6 * 3600

# Decision record: 392 bytes, little-endian, explicit padding (static_assert in decision_log.hpp).
REC = struct.Struct("<BBBBHB3sB5xII" + "q" + "QQQQ" + "q" + "QQQ" + "qq" + "Q" + "II" + "3Q3Q3Q" + "96s32s64s")
assert REC.size == 392
MAGIC = b"ARBDEC01"
REASONS = ["none", "base_price_unknown", "no_profit", "cu_over_budget", "below_min_net", "guard_overflow",
           "guard_above_quote", "below_min_ev", "pool_conflict", "slot_cap", "send_lag", "sizer_failed",
           "boundary", "flash_disabled", "risk_block", "tx_build_failed", "sign_guard", "sizer_no_room",
           "sizer_no_profit", "sizer_partial_fill", "sizer_quote_error", "sizer_zero_out"]
ACTIONS = {0: "skipped", 1: "sent", 2: "dry_run"}
DEXES = {0: "raydium_v4", 1: "whirlpool", 2: "cpmm", 3: "dlmm", 4: "clmm", 5: "pumpswap"}
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
WSOL = "So11111111111111111111111111111111111111112"
BASE_DECIMALS = {WSOL: 9, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": 6}
BASE_NAMES = {WSOL: "SOL", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}
# Cumulative counters shown as per-second rates.
RATE_KEYS = ["arb_feed_frames", "arb_feed_bytes", "arb_engine_pool_updates", "arb_engine_cycles_seen",
             "arb_engine_prefilter_pass", "arb_engine_sized", "arb_engine_candidates"]
UNITS = ["jito-arb.service", "jito-arb-discover.timer", "jito-arb-discover.service", "jito-arb-recorder.service"]


def b58(raw):
    n = int.from_bytes(raw, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + s


class Sampler(threading.Thread):
    def __init__(self, url):
        super().__init__(daemon=True)
        self.url = url
        self.samples = collections.deque(maxlen=KEEP_S // SAMPLE_S)
        self.lock = threading.Lock()
        self.error = None

    def scrape(self):
        with urllib.request.urlopen(self.url, timeout=3) as r:
            text = r.read().decode()
        out = {}
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                try:
                    out[parts[0]] = float(parts[1])
                except ValueError:
                    pass
        return out

    def run(self):
        while True:
            t = time.time()
            try:
                m = self.scrape()
                with self.lock:
                    self.samples.append((t, m))
                self.error = None
            except Exception as e:  # the bot restarting, or not up yet
                self.error = str(e)
            time.sleep(max(0.5, SAMPLE_S - (time.time() - t)))

    def snapshot(self):
        with self.lock:
            return list(self.samples)


def rates(samples, window_s=60):
    """Per-second rates of the cumulative counters over the last `window_s` (reset-safe)."""
    if len(samples) < 2:
        return {}
    t1, m1 = samples[-1]
    t0, m0 = samples[0]
    for t, m in reversed(samples):
        if t1 - t >= window_s:
            t0, m0 = t, m
            break
    dt = t1 - t0
    out = {}
    for k in RATE_KEYS:
        if k in m1 and k in m0 and dt > 0:
            d = m1[k] - m0[k]
            out[k] = d / dt if d >= 0 else None
    return out


def unit_states():
    out = {}
    for u in UNITS:
        try:
            r = subprocess.run(["systemctl", "is-active", u], capture_output=True, text=True, timeout=3)
            out[u] = r.stdout.strip() or r.stderr.strip()
        except FileNotFoundError:
            out[u] = "no systemd"  # running off the server (development)
        except Exception as e:
            out[u] = "unknown: %s" % e
    try:
        r = subprocess.run(["systemctl", "show", "jito-arb-discover.service", "-p", "ExecMainExitTimestamp", "-p", "Result",
                            "--no-pager"], capture_output=True, text=True, timeout=3)
        props = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
        out["last_discovery"] = props.get("ExecMainExitTimestamp", "")
        out["discovery_result"] = props.get("Result", "")
        r = subprocess.run(["systemctl", "is-enabled", "jito-arb-recorder.service"], capture_output=True, text=True, timeout=3)
        out["recorder_enabled"] = r.stdout.strip()
    except Exception:
        out.setdefault("last_discovery", "")
    return out


def pools_summary(data_dir):
    path = os.path.join(data_dir, "pools.json")
    try:
        st = os.stat(path)
        with open(path) as f:
            d = json.load(f)
    except Exception as e:
        return {"error": str(e)}
    pools = d.get("pools", [])
    kinds = collections.Counter(p.get("kind") for p in pools)
    top = sorted(pools, key=lambda p: -p.get("tvl_sol", 0))[:12]
    return {
        "count": len(pools), "by_kind": dict(kinds), "updated": st.st_mtime,
        "tvl_sol_total": round(sum(p.get("tvl_sol", 0) for p in pools), 1),
        "top": [{"kind": p.get("kind"), "address": p.get("address"), "tvl_sol": round(p.get("tvl_sol", 0), 1),
                 "mint_a": p.get("mint_a"), "mint_b": p.get("mint_b")} for p in top],
    }


def read_decisions(data_dir, hours):
    files = sorted(glob.glob(os.path.join(data_dir, "decisions", "decisions-*.bin")), key=os.path.getmtime)[-72:]
    cutoff_ms = (time.time() - hours * 3600) * 1000
    recs = []
    for path in files:
        try:
            with open(path, "rb") as f:
                if f.read(8) != MAGIC:
                    continue
                while True:
                    b = f.read(REC.size)
                    if len(b) < REC.size:
                        break
                    v = REC.unpack(b)
                    if v[0] != 1 or v[10] < cutoff_ms:  # kind = candidate; wall_ms
                        continue
                    recs.append(v)
        except OSError:
            continue
    by_reason = collections.Counter()
    by_day = collections.defaultdict(lambda: {"candidates": 0, "dispatched": 0, "gross_lamports": 0, "tip_lamports": 0,
                                              "net_lamports": 0, "ev_lamports": 0})
    rows = []
    for v in recs:
        (kind, hops, action, reason, route_class, outcome, dex, mask, cycle_id, _pad, wall_ms, slot, x, final_out,
         flash_fee, profit, gross, tip, fee, net, ev, min_out, cu, p_land) = v[:24]
        base = b58(v[-2])
        day = datetime.datetime.utcfromtimestamp(wall_ms / 1000).strftime("%Y-%m-%d")
        d = by_day[day]
        d["candidates"] += 1
        if action != 0:
            d["dispatched"] += 1
            d["gross_lamports"] += gross
            d["tip_lamports"] += tip
            d["net_lamports"] += net
            d["ev_lamports"] += ev
        else:
            by_reason[REASONS[reason] if reason < len(REASONS) else str(reason)] += 1
        dec = BASE_DECIMALS.get(base, 9)
        rows.append({
            "t": wall_ms, "slot": slot, "cycle": cycle_id, "hops": hops,
            "route": " → ".join(DEXES.get(dex[i], "?") for i in range(min(hops, 3))),
            "base": BASE_NAMES.get(base, base[:4]), "size": x / 10 ** dec, "gross_sol": gross / 1e9, "tip_sol": tip / 1e9,
            "net_sol": net / 1e9, "ev_sol": ev / 1e9, "action": ACTIONS.get(action, str(action)),
            "reason": REASONS[reason] if action == 0 and reason < len(REASONS) else "",
        })
    rows.sort(key=lambda r: -r["t"])
    return {"hours": hours, "records": len(recs), "files": len(files), "by_day": dict(sorted(by_day.items())),
            "skipped_by_reason": dict(by_reason.most_common()), "latest": rows[:60]}


class Handler(BaseHTTPRequestHandler):
    sampler = None
    data_dir = "/var/lib/jito-arb"

    def log_message(self, fmt, *args):  # quiet: the tunnel polls often
        pass

    def send(self, code, body, ctype):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(b)

    def json(self, obj):
        self.send(200, json.dumps(obj, separators=(",", ":")), "application/json")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self.send(200, f.read(), "text/html; charset=utf-8")
            elif u.path == "/api/state":
                s = self.sampler.snapshot()
                self.json({
                    "now": time.time(), "metrics": s[-1][1] if s else {}, "sampled_at": s[-1][0] if s else None,
                    "scrape_error": self.sampler.error, "rates": rates(s), "units": unit_states(),
                    "pools": pools_summary(self.data_dir),
                })
            elif u.path == "/api/history":
                minutes = max(5, min(360, int(q.get("minutes", ["60"])[0])))
                s = [x for x in self.sampler.snapshot() if x[0] >= time.time() - minutes * 60]
                keys = ["arb_feed_update_age_ms", "arb_pools_quotable", "arb_feed_shards_up", "arb_feed_reconnects",
                        "arb_feed_lag_us_p50", "arb_feed_lag_us_p99", "arb_slot_offset_us_p50", "arb_slot_offset_us_p99",
                        "arb_detect_to_signed_us_p50", "arb_detect_to_signed_us_p99"]
                series = {"t": [t for t, _ in s]}
                for k in keys:  # the bot reports -1 for "nothing yet"
                    series[k] = [m.get(k) if m.get(k, -1) >= 0 else None for _, m in s]
                for k in RATE_KEYS:  # per-sample rates
                    vals = [None]
                    for (ta, ma), (tb, mb) in zip(s, s[1:]):
                        d = (mb.get(k, 0) - ma.get(k, 0)) / (tb - ta) if tb > ta else None
                        vals.append(d if d is not None and d >= 0 else None)
                    series[k + "_rate"] = vals
                self.json(series)
            elif u.path == "/api/decisions":
                hours = max(1, min(24 * 14, int(q.get("hours", ["24"])[0])))
                self.json(read_decisions(self.data_dir, hours))
            else:
                self.send(404, "not found", "text/plain")
        except BrokenPipeError:
            pass
        except Exception as e:
            self.send(500, json.dumps({"error": str(e)}), "application/json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--metrics", default="http://127.0.0.1:9100/metrics")
    ap.add_argument("--data", default="/var/lib/jito-arb")
    a = ap.parse_args()
    Handler.sampler = Sampler(a.metrics)
    Handler.sampler.start()
    Handler.data_dir = a.data
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    print("jito-arb ui on http://%s:%d (metrics %s, data %s)" % (a.bind, a.port, a.metrics, a.data), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
