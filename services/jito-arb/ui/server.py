#!/usr/bin/env python3
"""jito-arb dashboard: a read-only view of the running bot (Python 3.9 stdlib only).

It samples the bot's Prometheus endpoint every 5 s into a 6-hour ring buffer, reads the decision logs
(src/obs/decision_log.hpp layout) and the discovery pools file, and serves one page plus JSON:

  /                 the dashboard (index.html next to this file)
  /api/state        latest metrics, rates, service states, discovery summary
  /api/history      sampled series for the charts (?minutes=60)
  /api/decisions    decision-log counts and skip reasons (?hours=24)
  /api/sends        sent bundles from the send log: Jito acceptance, post-send check, landing

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
import re
import struct
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_NAME = re.compile(r"^Anthropic(Sans|Mono)-(Roman|Italic)-Web\.woff2$")
SAMPLE_S = 5
KEEP_S = 6 * 3600

# Decision record: 392 bytes, little-endian, explicit padding (static_assert in decision_log.hpp).
REC = struct.Struct("<BBBBHB3sB5xII" + "q" + "QQQQ" + "q" + "QQQ" + "qq" + "Q" + "II" + "3Q3Q3Q" + "96s32s64s")
assert REC.size == 392
MAGIC = b"ARBDEC01"
REASONS = ["none", "base_price_unknown", "no_profit", "cu_over_budget", "below_min_net", "guard_overflow",
           "guard_above_quote", "below_min_ev", "pool_conflict", "slot_cap", "send_lag", "sizer_failed",
           "boundary", "flash_disabled", "risk_block", "tx_build_failed", "sign_guard", "sizer_no_room",
           "sizer_no_profit", "sizer_partial_fill", "sizer_quote_error", "sizer_zero_out", "implausible_profit",
           "quarantined", "not_armed", "leader_skipped"]
ACTIONS = {0: "skipped", 1: "sent", 2: "dry_run"}
DEXES = {0: "raydium_v4", 1: "whirlpool", 2: "cpmm", 3: "dlmm", 4: "clmm", 5: "pumpswap"}
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
WSOL = "So11111111111111111111111111111111111111112"
BASE_DECIMALS = {WSOL: 9, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": 6}
BASE_NAMES = {WSOL: "SOL", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}
# Cumulative counters shown as per-second rates.
RATE_KEYS = ["arb_feed_frames", "arb_feed_bytes", "arb_engine_pool_updates", "arb_engine_cycles_seen",
             "arb_engine_prefilter_pass", "arb_engine_sized", "arb_engine_candidates"]
# Helius usage counters (per RPC method, per stream account kind): rated like RATE_KEYS for the usage panel.
USAGE_PREFIXES = ("arb_rpc_calls_", "arb_rpc_bytes_in_", "arb_rpc_rate_limited_", "arb_feed_updates_", "arb_feed_bytes_")
UNITS = ["jito-arb.service", "jito-arb-discover.timer", "jito-arb-discover.service", "jito-arb-recorder.service"]


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
    usage = [k for k in m1 if k.startswith(USAGE_PREFIXES)]
    for k in RATE_KEYS + usage:
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


ROUTE_DEX = ["raydium_v4", "whirlpool", "cpmm", "dlmm", "clmm", "pumpswap", "?", "?"]


def route_name(cls):
    """route_class = hops << 8 | one bit per DEX kind on the route."""
    try:
        cls = int(cls)
    except (TypeError, ValueError):
        return "?"
    dexes = [ROUTE_DEX[i] for i in range(8) if cls & (1 << i)]
    return "%d-hop %s" % (cls >> 8, "+".join(dexes) or "?")


class SendsCache:
    """Append-only reader of the bot's sent log (obs.sent_log_path): one line per region send, per audit verdict
    and per Jito landing verdict. Joined per bundle by signature prefix and bundle id; survives bot restarts."""
    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.inode = None
        self.by_sig = collections.OrderedDict()
        self.sig_of_id = {}
        self.sender = collections.OrderedDict()  # plain-transaction (Helius Sender) sends, by signature prefix
        self.lock = threading.Lock()

    def update(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return
        if self.inode != st.st_ino or st.st_size < self.offset:
            self.__init__(self.path)
            self.inode = st.st_ino
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read()
        cut = data.rfind(b"\n") + 1  # leave a partial last line for the next read
        self.offset += cut
        for raw in data[:cut].splitlines():
            try:
                x = json.loads(raw)
            except ValueError:
                continue
            if x.get("path") == "sender":
                if "landing" in x:
                    b = self.sender.get(x.get("sig"))
                    if b is not None:
                        b["landing"] = x["landing"]
                        b["fee_paid"] = x.get("fee", 0)
                else:
                    self.sender[x["sig"]] = {"t": x["t"], "accepted": bool(x.get("accepted")), "stale": bool(x.get("stale")),
                                             "fee": x.get("fee", 0), "cu_price": x.get("cu_price"), "landing": None, "fee_paid": 0}
                    while len(self.sender) > 50000:
                        self.sender.popitem(last=False)
                continue
            if "sent_region" in x:
                b = self.by_sig.get(x["sig"])
                if b is None:
                    b = self.by_sig[x["sig"]] = {"t": x["t"], "slot": x.get("slot"), "route": route_name(x.get("class")),
                                                 "leader_kind": x.get("kind"), "leader_place": x.get("place", ""),
                                                 "regions": [], "accepted": False, "audit": None, "landing": None}
                b["regions"].append({"region": x["sent_region"], "status": x.get("status"), "rtt_ms": x.get("rtt_ms"),
                                     "stale": x.get("stale")})
                if x.get("id"):
                    b["accepted"] = True
                    self.sig_of_id[x["id"]] = x["sig"]
            elif "audit" in x and x.get("sig") in self.by_sig:
                self.by_sig[x["sig"]]["audit"] = x["audit"]
            elif "landing" in x:
                sig = self.sig_of_id.get(x.get("id"))
                if sig in self.by_sig:
                    self.by_sig[sig]["landing"] = x["landing"]

    def summary(self, recent=15):
        with self.lock:
            self.update()
            bundles = list(self.by_sig.values())
        day = time.strftime("%Y-%m-%d", time.gmtime())
        tot = collections.Counter()
        kinds = collections.defaultdict(collections.Counter)
        regions = collections.defaultdict(collections.Counter)
        for b in bundles:
            k = b["leader_kind"] or "unknown"
            for c in (tot, kinds[k]):
                c["sent"] += 1
                c["accepted"] += b["accepted"]
                c["landed"] += b["landing"] == "landed"
                c["not_landed"] += b["landing"] == "not_landed"
                c["rejected"] += b["landing"] == "rejected"
                c["audited"] += b["audit"] is not None
                c["edge_gone"] += b["audit"] == "min_out"
                c["tx_failed"] += b["audit"] == "failed"
            tot["today"] += time.strftime("%Y-%m-%d", time.gmtime(b["t"] / 1000)) == day
            for r in b["regions"]:
                rc = regions[r["region"]]
                rc["sends"] += 1
                rc["accepted"] += r["status"] == 200
        with self.lock:
            plain = list(self.sender.values())
        sc = collections.Counter()
        for b in plain:
            sc["sent"] += 1
            sc["accepted"] += b["accepted"]
            sc["stale"] += b["stale"]
            sc[b["landing"] or ("pending" if b["accepted"] else "refused")] += 1
            sc["fees_paid"] += b["fee_paid"] or 0
            if b["landing"] == "failed" and time.strftime("%Y-%m-%d", time.gmtime(b["t"] / 1000)) == day:
                sc["failed_fees_today"] += b["fee_paid"] or 0
        return {"since": bundles[0]["t"] if bundles else None, "totals": dict(tot), "sender": dict(sc),
                "by_kind": {k: dict(v) for k, v in kinds.items()}, "by_region": {k: dict(v) for k, v in regions.items()},
                "recent": bundles[-recent:][::-1]}


_SENDS = {}


class DecisionCache:
    """Append-only reader of the decision logs: per-second counts of logged and dispatched candidates and the skip
    reasons. Profit columns are not summed: quoted profits of bundles that did not land are not results."""
    def __init__(self):
        self.offsets = {}
        self.seconds = {}

    def update(self, files):
        stats = {}
        for path in files:
            try:
                st = os.stat(path)
                stats[path] = (st.st_ino, st.st_size)
            except OSError:
                pass
        if any(path not in stats or stats[path][0] != inode or stats[path][1] < offset
               for path, (inode, offset) in self.offsets.items()):
            self.__init__()
        for path, (inode, size) in stats.items():
            offset = self.offsets.get(path, (inode, 8))[1]
            if size <= offset and path in self.offsets:
                continue
            try:
                with open(path, "rb") as f:
                    if f.read(8) != MAGIC:
                        continue
                    f.seek(offset)
                    while True:
                        block = f.read(REC.size * 4096)
                        count = len(block) // REC.size
                        for v in REC.iter_unpack(block[:count * REC.size]):
                            if v[0] != 1:
                                continue
                            second = v[10] // 1000
                            d, reasons = self.seconds.setdefault(second, ([0, 0], collections.Counter()))
                            d[0] += 1
                            if v[2] != 0:
                                d[1] += 1
                            else:
                                reasons[REASONS[v[3]] if v[3] < len(REASONS) else str(v[3])] += 1
                        offset += count * REC.size
                        if len(block) < REC.size * 4096:
                            break
                self.offsets[path] = (inode, offset)
            except OSError:
                continue
        oldest = int(time.time()) - 14 * 86400
        self.seconds = {s: b for s, b in self.seconds.items() if s >= oldest}

    def result(self, hours, file_count):
        cutoff = int(time.time() - hours * 3600)
        by_day = collections.defaultdict(lambda: {"candidates": 0, "dispatched": 0})
        by_reason = collections.Counter()
        for second, (d, reasons) in self.seconds.items():
            if second < cutoff:
                continue
            day = datetime.datetime.utcfromtimestamp(second).strftime("%Y-%m-%d")
            by_day[day]["candidates"] += d[0]
            by_day[day]["dispatched"] += d[1]
            by_reason.update(reasons)
        return {"hours": hours, "files": file_count, "by_day": dict(sorted(by_day.items())),
                "skipped_by_reason": dict(by_reason.most_common())}


_DECISION_CACHES = {}
_DECISION_LOCK = threading.Lock()


def read_decisions(data_dir, hours):
    files = sorted(glob.glob(os.path.join(data_dir, "decisions", "decisions-*.bin")), key=os.path.getmtime)[-72:]
    with _DECISION_LOCK:
        cache = _DECISION_CACHES.setdefault(os.path.abspath(data_dir), DecisionCache())
        cache.update(files)
        return cache.result(hours, len(files))

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
            elif u.path.startswith("/fonts/"):
                # Anthropic Sans and Mono (.woff2), copied to ui/fonts/ on the host (never committed: the font is proprietary).
                # Only plain file names of the expected form; the page falls back to the system font without them.
                name = u.path[len("/fonts/"):]
                path = os.path.join(HERE, "fonts", name)
                if not FONT_NAME.match(name) or not os.path.isfile(path):
                    self.send(404, "not found", "text/plain")
                else:
                    with open(path, "rb") as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "font/woff2")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    self.wfile.write(body)
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
            elif u.path == "/api/sends":
                path = os.path.join(self.data_dir, "sent.jsonl")
                cache = _SENDS.setdefault(path, SendsCache(path))
                self.json(cache.summary())
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
