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
    universe = None
    symbols = [s.upper() for s in _csv(args.symbols)] if args.symbols else None
    if symbols is None:
        hist = paths.root / "universe" / "history.json"
        if not hist.exists():
            raise SystemExit(
                "no universe history: run `zt data universe --from ...` or pass --symbols"
            )
        universe = json.loads(hist.read_text())["months"]
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
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
