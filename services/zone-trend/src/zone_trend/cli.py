"""``zt`` command line. Subcommands are added milestone by milestone (see PLAN.md).

M1 data commands::

    zt data symbols
    zt data sync --symbols BTCUSDT,ETHUSDT --from 2020-01 \
        --datasets klines,markPriceKlines,fundingRate
    zt data sync --symbols all --datasets klines --intervals 1d   # universe ranking input
    zt data exchange-info
    zt data validate
    zt data universe --asof 2024-06-01
    zt data universe --from 2020-01 --to 2026-10   # writes universe/history.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pyarrow.parquet as pq

from zone_trend.config import DEFAULT_PATH, load_config
from zone_trend.data import vision
from zone_trend.data.exchange import fetch_exchange_info, latest_filters
from zone_trend.data.resample import resample_main
from zone_trend.data.store import DataPaths, read_snapshot, write_snapshot
from zone_trend.data.sync import DEFAULT_INTERVALS, sync
from zone_trend.data.universe import DailyCache, is_usdt_perp, month_starts, universe_at
from zone_trend.data.validate import validate_symbol

DEFAULT_FROM = "2019-09"  # BTCUSDT perpetual history starts here


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _check_config(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    print(f"ok: {args.config} (mode={cfg.mode.value})")
    return 0


def _data_symbols(args: argparse.Namespace) -> int:
    with httpx.Client(timeout=60) as client:
        symbols = vision.list_symbols(client)
    print("\n".join(symbols))
    print(f"# {len(symbols)} symbols", file=sys.stderr)
    return 0


def _data_sync(args: argparse.Namespace) -> int:
    paths = DataPaths.from_env(args.data_dir)
    if args.symbols == "all":  # every USDT perpetual on Vision, delisted ones included
        with httpx.Client(timeout=60) as client:
            symbols = [s for s in vision.list_symbols(client) if is_usdt_perp(s)]
    else:
        symbols = [s.upper() for s in _csv(args.symbols)]
    datasets = {}
    for name in _csv(args.datasets):
        if name not in vision.DATASETS:
            raise SystemExit(f"unknown dataset {name!r}; choose from {sorted(vision.DATASETS)}")
        intervals = DEFAULT_INTERVALS[name]
        if args.intervals and intervals:
            intervals = tuple(_csv(args.intervals))
        datasets[name] = intervals
    results = sync(
        paths,
        symbols,
        datasets,
        args.start,
        args.end,
        workers=args.workers,
        keep_raw=not args.drop_raw,
    )
    errors = [r for r in results if r.error]
    built = sum(len(r.built) for r in results)
    for r in results:
        if r.built or r.error:
            status = f"error: {r.error}" if r.error else f"built {len(r.built)}"
            print(f"{r.dataset:17s} {r.symbol:14s} {r.tf:5s} {status} (kept {r.skipped})")
    print(f"# {built} month files built, {len(errors)} series failed, root {paths.root}")
    return 1 if errors else 0


def _data_resample_15m(args: argparse.Namespace) -> int:
    symbols = [s.upper() for s in _csv(args.symbols)]
    for sym, (built, skipped) in resample_main(args.data_dir, symbols, args.force).items():
        print(f"{sym:14s} built {built} month files (kept {skipped})")
    return 0


def _data_exchange_info(args: argparse.Namespace) -> int:
    paths = DataPaths.from_env(args.data_dir)
    print(fetch_exchange_info(paths))
    return 0


def _data_validate(args: argparse.Namespace) -> int:
    paths = DataPaths.from_env(args.data_dir)
    filters = latest_filters(paths)
    symbols = (
        [s.upper() for s in _csv(args.symbols)]
        if args.symbols
        else sorted({p.parent.parent.name for p in paths.parquet.glob("*/*/*/*.parquet")})
    )
    quarantine: dict[str, dict[str, list[str]]] = {}
    report: dict[str, dict] = {"exchange_info": filters.snapshot, "symbols": {}}
    for symbol in symbols:
        rep = validate_symbol(
            paths,
            symbol,
            tick=filters.get(symbol, {}).get("tick") or None,
            strict=args.strict,
        )
        if rep.quarantine:
            quarantine[symbol] = rep.quarantine
        report["symbols"][symbol] = {
            **{k: v for k, v in asdict(rep).items() if k not in ("quarantine", "flags", "symbol")},
            "quarantined_days": len(rep.quarantine),
            "flagged_days": len(rep.flags),
            "flags": rep.flags,
        }
        if not args.quiet or rep.quarantine:
            missing = sum(s.missing_bars for s in rep.series)
            print(
                f"{symbol:14s} series {len(rep.series)}  missing bars {missing:6d}  "
                f"ohlc {sum(s.ohlc_bad for s in rep.series)}  "
                f"resample close/vol {rep.resample_close_bad}/{rep.resample_volume_bad}  "
                f"mark>2% h {rep.mark_divergent_hours}  quarantined days {len(rep.quarantine)}"
            )
    parquet_files = paths.parquet.glob("*/*/*/*.parquet")
    files = [(p, pq.ParquetFile(p).metadata.num_rows) for p in parquet_files]
    snapshot_id = write_snapshot(paths, files, quarantine, report)
    print(f"# snapshot {snapshot_id}: {len(files)} files, {len(quarantine)} quarantined symbols")
    return 0


def _data_universe(args: argparse.Namespace) -> int:
    paths = DataPaths.from_env(args.data_dir)
    cfg = load_config(args.config)
    try:
        quarantine = read_snapshot(paths)["quarantine"]
    except FileNotFoundError:
        quarantine = {}
        print("# no snapshot: run `zt data validate` to rank with quarantine", file=sys.stderr)
    filters = latest_filters(paths)
    daily = DailyCache(paths)
    kwargs = dict(
        size=cfg.universe.size,
        min_listing_days=cfg.universe.min_listing_days,
        exclude=tuple(cfg.universe.exclude),
        coins_only=cfg.universe.coins_only,
        quarantine=quarantine,
        filters=filters,
        daily=daily,
    )
    if args.asof:
        res = universe_at(paths, args.asof, **kwargs)
        for i, row in enumerate(res.ranked, 1):
            rng = f"{row['range_1h']:.4f}" if row["range_1h"] is not None else "   n/a"
            vol = row["median_quote_volume"]
            print(f"{i:3d} {row['symbol']:14s} vol {vol:16,.0f}  range1h {rng}  {row['outcome']}")
        if res.missing_1h:
            print(f"# no 1h data (range check skipped): {','.join(res.missing_1h)}")
        return 0

    end = args.end or f"{datetime.now(UTC):%Y-%m}"
    history = {}
    union: set[str] = set()
    missing: set[str] = set()
    for month in month_starts(args.start, end):
        res = universe_at(paths, month, **kwargs)
        history[month.strftime("%Y-%m")] = res.symbols
        union |= set(res.symbols)
        missing |= set(res.missing_1h)
        print(f"{month:%Y-%m} {' '.join(res.symbols)}")
    target = paths.root / "universe" / "history.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"exchange_info": filters.snapshot, "months": history}, indent=1))
    print(f"# union ({len(union)}): {','.join(sorted(union))}")
    if missing:
        print(f"# pool symbols without 1h data ({len(missing)}): {','.join(sorted(missing))}")
    print(f"# wrote {target}")
    return 0


def _backtest(args: argparse.Namespace) -> int:
    # Imported here so `zt data ...` does not pay for the backtester's imports.
    from zone_trend.backtest.engine import RunSpec, run
    from zone_trend.backtest.metrics import summary
    from zone_trend.backtest.report import write_run

    paths = DataPaths.from_env(args.data_dir)
    cfg = load_config(args.config)
    universe, symbols = _universe_or_symbols(paths, args.symbols)
    end = args.end or f"{datetime.now(UTC):%Y-%m}"
    spec = RunSpec(
        start=args.start,
        end=end,
        symbols=symbols,
        universe=universe,
        kind="ema" if args.benchmark else "zone",
        equity=args.equity,
        slippage_mult=args.slippage_mult,
        workers=args.workers,
        use_cache=not args.no_cache,
    )
    result = run(paths, cfg, spec)
    out = write_run(result, cfg, args.runs_dir)
    stats = summary(result.trades, result.equity)
    keys = (
        "trades",
        "win_rate",
        "profit_factor",
        "expectancy_r",
        "cagr",
        "max_drawdown",
        "sharpe_daily",
        "end_equity",
    )
    print(
        "  ".join(
            f"{k} {stats[k]:.4g}" if isinstance(stats[k], float) else f"{k} {stats[k]}"
            for k in keys
        )
    )
    print(f"# {out}/report.html")
    return 0


def _universe_or_symbols(paths: DataPaths, symbols_arg: str | None):
    symbols = [s.upper() for s in _csv(symbols_arg)] if symbols_arg else None
    if symbols is not None:
        return None, symbols
    hist = paths.root / "universe" / "history.json"
    if not hist.exists():
        raise SystemExit("no universe history: run `zt data universe --from ...` or pass --symbols")
    return json.loads(hist.read_text())["months"], None


def _last_full_month() -> str:
    import pandas as pd

    return str(pd.Period(datetime.now(UTC).strftime("%Y-%m"), freq="M") - 1)


def _wf(args: argparse.Namespace) -> int:
    from zone_trend.backtest.validation import acceptance
    from zone_trend.backtest.walkforward import walk_forward

    paths = DataPaths.from_env(args.data_dir)
    cfg = load_config(args.config)
    universe, symbols = _universe_or_symbols(paths, args.symbols)
    end = args.end or _last_full_month()
    out = args.out or Path("runs") / f"wf-{args.start}-{end}"
    walk_forward(paths, cfg, universe, symbols, args.start, end, out, args.equity, args.workers)
    _print_acceptance(acceptance(out))
    print(f"# {out}")
    return 0


def _print_acceptance(report: dict) -> None:
    for row in report["criteria"]:
        print(f"{row['status']:8s} {row['criterion']:48s} {row['value']}  ({row['threshold']})")
    print(f"verdict: {report['verdict']}")


def _accept(args: argparse.Namespace) -> int:
    from zone_trend.backtest.validation import acceptance

    report = acceptance(args.wf)
    _print_acceptance(report)
    return 0 if report["verdict"] == "pass" else 1


def _holdout(args: argparse.Namespace) -> int:
    from zone_trend.backtest.validation import run_holdout

    stats = run_holdout(DataPaths.from_env(args.data_dir), args.wf, force=args.force)
    print({k: stats[k] for k in ("trades", "profit_factor", "expectancy_r", "max_drawdown")})
    return 0


def _ablate(args: argparse.Namespace) -> int:
    from zone_trend.backtest.validation import ABLATIONS, run_ablation

    comps = list(ABLATIONS) if args.component == "all" else _csv(args.component)
    for comp in comps:
        if comp not in ABLATIONS:
            raise SystemExit(f"unknown component {comp!r}; choose from {', '.join(ABLATIONS)}")
        e = run_ablation(DataPaths.from_env(args.data_dir), args.wf, comp, args.workers)
        print(
            f"{comp:14s} default {e['default']}  ablated {e['ablated']}  "
            f"adds={e['adds_expectancy']}"
        )
    return 0


def _mc(args: argparse.Namespace) -> int:
    import pandas as pd

    from zone_trend.backtest.montecarlo import monte_carlo

    d: Path = args.run
    if (d / "oos_trades.parquet").exists():
        trades, equity = d / "oos_trades.parquet", d / "oos_equity.parquet"
    else:
        trades, equity = d / "trades.parquet", d / "equity.parquet"
    res = monte_carlo(pd.read_parquet(trades), pd.read_parquet(equity), n_paths=args.paths)
    print(json.dumps(res, indent=1))
    return 0


def _episodes(args: argparse.Namespace) -> int:
    from zone_trend.backtest.episodes import collect, write_episodes

    paths = DataPaths.from_env(args.data_dir)
    eps = collect(
        paths, args.symbol.upper(), load_config(args.config), args.start, args.end, args.limit
    )
    out = args.out or Path("episodes") / args.symbol.upper()
    print(f"{len(eps)} episodes -> {write_episodes(eps, out)}")
    return 0


def _shadow(args: argparse.Namespace) -> int:
    import asyncio

    from zone_trend.live.main import run_service

    asyncio.run(run_service("shadow"))
    return 0


def _live(args: argparse.Namespace) -> int:
    import asyncio

    from zone_trend.live.main import run_service

    asyncio.run(run_service("live", resume=args.resume))
    return 0


def _kill(args: argparse.Namespace) -> int:
    import asyncio

    from zone_trend.live.main import run_kill

    for line in asyncio.run(run_kill()):
        print(line)
    return 0


def _parity(args: argparse.Namespace) -> int:
    from zone_trend.live.main import parity

    res = parity(args.db, args.data_dir, args.start, args.end, args.config)
    print(json.dumps({k: v for k, v in res.items() if not k.startswith("only_")}, indent=1))
    for k in ("only_live", "only_backtest"):
        if res[k]:
            print(f"{k}: {', '.join(x[:12] for x in res[k][:20])}")
    return 0 if res["match_rate"] >= 0.95 else 1


def _status(args: argparse.Namespace) -> int:
    import uvicorn

    from zone_trend.live.status import create_app

    uvicorn.run(
        create_app(args.db, args.runs_dir), host=args.host, port=args.port, log_level="warning"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zt", description="Zone-trend strategy tooling")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("config", help="validate a config file")
    check.add_argument("--config", type=Path, default=DEFAULT_PATH)
    check.set_defaults(func=_check_config)

    data = sub.add_parser("data", help="Binance Vision data pipeline").add_subparsers(
        dest="data_command", required=True
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", type=Path, help="default: $ZT_DATA_DIR, else ./data")

    data.add_parser("symbols", help="list every USDⓈ-M symbol on Vision").set_defaults(
        func=_data_symbols
    )

    p = data.add_parser("sync", parents=[common], help="download and convert to Parquet")
    p.add_argument("--symbols", required=True, help="comma list, or 'all' (USDT perps)")
    p.add_argument("--from", dest="start", default=DEFAULT_FROM, help="YYYY-MM")
    p.add_argument("--to", dest="end", default=None, help="YYYY-MM (default: through today)")
    p.add_argument("--datasets", default="klines,markPriceKlines,fundingRate")
    p.add_argument("--intervals", default=None, help="override kline intervals, e.g. 1d")
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--drop-raw", action="store_true", help="delete zips after conversion")
    p.set_defaults(func=_data_sync)

    p = data.add_parser(
        "resample-15m", parents=[common], help="derive 15m months from synced 1m months"
    )
    p.add_argument("--symbols", required=True, help="comma list")
    p.add_argument("--force", action="store_true", help="rebuild months that exist")
    p.set_defaults(func=_data_resample_15m)

    p = data.add_parser("exchange-info", parents=[common], help="snapshot exchangeInfo")
    p.set_defaults(func=_data_exchange_info)

    p = data.add_parser("validate", parents=[common], help="checks, quarantine, snapshot")
    p.add_argument("--symbols", default=None, help="comma list (default: all on disk)")
    p.add_argument("--strict", action="store_true", help="also quarantine every flagged day")
    p.add_argument("--quiet", action="store_true", help="only print symbols with quarantine")
    p.set_defaults(func=_data_validate)

    p = data.add_parser("universe", parents=[common], help="point-in-time top-N universe")
    p.add_argument("--config", type=Path, default=DEFAULT_PATH)
    p.add_argument("--asof", type=date.fromisoformat, default=None)
    p.add_argument("--from", dest="start", default="2020-01")
    p.add_argument("--to", dest="end", default=None)
    p.set_defaults(func=_data_universe)

    p = sub.add_parser("backtest", parents=[common], help="event-driven portfolio backtest")
    p.add_argument("--config", type=Path, default=DEFAULT_PATH)
    p.add_argument("--from", dest="start", default="2021-01", help="YYYY-MM")
    p.add_argument("--to", dest="end", default=None, help="YYYY-MM inclusive (default: now)")
    p.add_argument("--symbols", default=None, help="fixed list (default: universe history)")
    p.add_argument("--benchmark", action="store_true", help="1D EMA 20/100 rule instead")
    p.add_argument("--equity", type=float, default=500.0)
    p.add_argument("--slippage-mult", type=float, default=1.0)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--no-cache", action="store_true", help="rebuild symbol tapes")
    p.add_argument("--runs-dir", type=Path, default=Path("runs"))
    p.set_defaults(func=_backtest)

    p = sub.add_parser("wf", parents=[common], help="walk-forward + acceptance table")
    p.add_argument("--config", type=Path, default=DEFAULT_PATH)
    p.add_argument("--from", dest="start", default="2021-01")
    p.add_argument("--to", dest="end", default=None, help="last month incl. holdout")
    p.add_argument("--symbols", default=None)
    p.add_argument("--equity", type=float, default=500.0)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--out", type=Path, default=None)
    p.set_defaults(func=_wf)

    p = sub.add_parser("accept", help="recompute the acceptance table for a wf dir")
    p.add_argument("--wf", type=Path, required=True)
    p.set_defaults(func=_accept)

    p = sub.add_parser("holdout", parents=[common], help="run the 9-month holdout, once")
    p.add_argument("--wf", type=Path, required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_holdout)

    p = sub.add_parser("ablate", parents=[common], help="switch one component off")
    p.add_argument("--wf", type=Path, required=True)
    p.add_argument("--component", default="all", help="comma list or 'all'")
    p.add_argument("--workers", type=int, default=4)
    p.set_defaults(func=_ablate)

    p = sub.add_parser("mc", help="Monte Carlo on a run or wf dir")
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--paths", type=int, default=10_000)
    p.set_defaults(func=_mc)

    p = sub.add_parser("episodes", parents=[common], help="render episodes for golden labels")
    p.add_argument("--config", type=Path, default=DEFAULT_PATH)
    p.add_argument("--symbol", required=True)
    p.add_argument("--from", dest="start", required=True)
    p.add_argument("--to", dest="end", required=True)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--out", type=Path, default=None)
    p.set_defaults(func=_episodes)

    p = sub.add_parser("shadow", help="run the strategy on live data, paper fills, no orders")
    p.set_defaults(func=_shadow)

    p = sub.add_parser("live", help="trade real money (needs ZT_LIVE_ACK and mainnet keys)")
    p.add_argument("--resume", action="store_true", help="clear a halt after manual review")
    p.set_defaults(func=_live)

    p = sub.add_parser("kill", help="close every zone-trend position, cancel orders, halt")
    p.set_defaults(func=_kill)

    p = sub.add_parser("parity", parents=[common], help="shadow signals vs backtest replay")
    p.add_argument("--db", default="sqlite:///state/zt.db")
    p.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    p.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD (exclusive)")
    p.add_argument("--config", type=Path, default=DEFAULT_PATH)
    p.set_defaults(func=_parity)

    p = sub.add_parser("status", help="read-only dashboard, /health and /metrics")
    p.add_argument("--db", default="sqlite:///state/zt.db")
    p.add_argument("--runs-dir", type=Path, default=Path("runs"))
    p.add_argument("--host", default="127.0.0.1", help="loopback by default; tunnel to expose")
    p.add_argument("--port", type=int, default=8060)
    p.set_defaults(func=_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
