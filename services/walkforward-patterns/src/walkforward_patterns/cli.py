"""wfp: run / check / status / flatten / disable / enable.

    wfp check              hub reachable, account alias present and tradable, quotes + M1 candles for the 3 symbols,
                           and the trendbar close vs the quote bid (the engine assumes bid bars, as in the research)
    wfp run                warm up and trade (systemd unit walkforward-patterns)
    wfp status             per-strategy table from the journal
    wfp flatten --confirm  close every wf_* position on the account (global kill; flatten is 501 on cTrader)
    wfp disable S07 "why"  / wfp enable S07
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime

from . import config as cfgmod
from .hub import Hub
from .journal import Journal
from .settings import Settings


def _ctx():
    s = Settings()
    cfg = cfgmod.load(s.config_path)
    hub = Hub(s.hub_url, s.hub_api_key.get_secret_value(), s.account)
    return s, cfg, hub


def cmd_check(_a) -> int:
    s, cfg, hub = _ctx()
    ok = True
    acc = [a for a in hub.accounts() if a.get("alias") == s.account]
    if not acc:
        print(f"FAIL account alias {s.account!r} not served by the hub")
        return 1
    a = acc[0]
    print(f"account {s.account}: env={a.get('environment')} connected={a.get('connected')} "
          f"order_entry_enabled={a.get('order_entry_enabled')} rights={a.get('broker_access_rights')}")
    if a.get("is_live"):
        print("WARN this alias is a LIVE account; the plan is an FTMO demo")
    for sym in cfg.symbols:
        q = hub.quote(sym)
        c = hub.candles(sym, 3)
        if not c:
            print(f"FAIL {sym}: no candles")
            ok = False
            continue
        last = c[-1]
        line = f"{sym}: last M1 end {last['ts']} close {last['close']}"
        if q:
            line += f" | quote bid {q['bid']} ask {q['ask']} spread {q.get('spread')}"
            d_bid, d_ask = abs(last["close"] - q["bid"]), abs(last["close"] - q["ask"])
            line += "  (close nearer bid)" if d_bid <= d_ask else "  (close nearer ASK - check bar side)"
        print(line)
    return 0 if ok else 1


def cmd_run(_a) -> int:
    from ta_notify import SyncNotifier

    from .live import Runner
    from .status import serve

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s, cfg, hub = _ctx()
    j = Journal(s.db_path)
    if j.get("started_at") is None:
        j.put("started_at", datetime.now(UTC).isoformat(timespec="seconds"))
    serve(j, cfg, s.status_host, s.status_port)
    notifier = SyncNotifier(s, source="walkforward-patterns")
    runner = Runner(s, cfg, hub, j, notifier)
    j.event("info", "start", f"walkforward-patterns starting on {s.account} (dry_run={s.dry_run})")
    runner.run_forever()
    return 0


def cmd_status(_a) -> int:
    from .status import snapshot

    s, cfg, _ = _ctx()
    snap = snapshot(Journal(s.db_path), cfg)
    print(f"heartbeat {snap['heartbeat']}  equity {snap['equity']}")
    print(f"{'id':4} {'sym':6} {'n':>4} {'win':>5} {'cumR':>7} {'meanR':>7} {'bt':>6} {'/mo':>5} {'bt/mo':>5} status")
    for r in snap["strategies"]:
        win = f"{100 * r['win']:.0f}%" if r["win"] is not None else ""
        mean = f"{r['mean_R']:+.3f}" if r["mean_R"] is not None else ""
        print(f"{r['id']:4} {r['symbol']:6} {r['trades']:4d} {win:>5} {r['cum_R']:+7.2f} {mean:>7} "
              f"{r['backtest_mean_R']:+6.3f} {r['rate_per_month']:5.1f} {r['backtest_per_month']:5.1f} {r['status']}"
              + (" OPEN" if r["open"] else ""))
    return 0


def cmd_flatten(a) -> int:
    if not a.confirm:
        print("refusing without --confirm")
        return 2
    s, cfg, hub = _ctx()
    j = Journal(s.db_path)
    rc = 0
    for p in hub.positions():
        label = p.get("label") or ""
        if not label.startswith("wf_"):
            continue
        out = hub.wait(hub.close_position(source=label, position_id=int(p["position_id"]),
                                          lots=float(p["volume_lots"])), 20)
        print(p["position_id"], label, out.state, out.error or "")
        rc |= out.state != "succeeded"
        for r in j.open_trades():
            if r["position_id"] == int(p["position_id"]):
                j.update_trade(r["id"], status="closing", exit_request="MANUAL")
    j.event("warn", "flatten", "wfp flatten run by the operator")
    return rc


def cmd_disable(a, flag: bool = True) -> int:
    s, cfg, _ = _ctx()
    if a.id not in {x.id for x in cfg.strategies}:
        print("unknown strategy", a.id)
        return 2
    j = Journal(s.db_path)
    j.set_disabled(a.id, flag, getattr(a, "reason", None) or ("manual" if flag else None))
    j.event("warn", "disable" if flag else "enable", f"{a.id} {'disabled' if flag else 'enabled'} by the operator")
    print(a.id, "disabled" if flag else "enabled")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wfp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("flatten")
    p.add_argument("--confirm", action="store_true")
    p.set_defaults(fn=cmd_flatten)
    p = sub.add_parser("disable")
    p.add_argument("id")
    p.add_argument("reason", nargs="?")
    p.set_defaults(fn=cmd_disable)
    p = sub.add_parser("enable")
    p.add_argument("id")
    p.set_defaults(fn=lambda a: cmd_disable(a, False))
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
