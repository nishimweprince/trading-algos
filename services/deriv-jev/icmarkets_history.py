"""Export IC Markets synthetic M1 bars and quote ticks from a dedicated MT5 terminal."""
import argparse
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import sys

import numpy as np

SYMBOLS = (
    'Stochastic_Core', 'Stochastic_Surge', 'Stochastic_Ultra',
    'Boom300', 'Boom600', 'Boom1000', 'Crash300', 'Crash600', 'Crash1000',
    'Jump50', 'Jump75', 'Jump100',
)
SERVER = 'ICMarketsSC-MT5-3'


def save_day(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.part')
    with temporary.open('wb') as stream:
        np.savez_compressed(stream, rows=rows)
    temporary.replace(path)


def export(mt, options):
    account = mt.account_info()
    terminal = mt.terminal_info()
    expected = Path(options.terminal).resolve().parent
    if not terminal or Path(terminal.path).resolve() != expected:
        raise RuntimeError('MT5 attached to an unexpected terminal path.')
    if not terminal.connected or not account or account.server != SERVER:
        raise RuntimeError(f'Sign in to {SERVER} in the research terminal first.')

    end = options.end
    if not options.start < end <= datetime.now(timezone.utc).date():
        raise ValueError('Use a start date before the exclusive end date, through the last complete UTC day.')

    failed = 0
    for symbol in options.symbols:
        if not mt.symbol_select(symbol, True):
            print(f'{symbol} unavailable: {mt.last_error()}', file=sys.stderr, flush=True)
            failed += 1
            continue
        info = mt.symbol_info(symbol)
        if not info:
            print(f'{symbol} specifications unavailable: {mt.last_error()}', file=sys.stderr, flush=True)
            failed += 1
            continue
        directory = options.output / symbol
        directory.mkdir(parents=True, exist_ok=True)
        keys = ('point', 'digits', 'trade_stops_level', 'trade_freeze_level', 'trade_tick_size',
                'trade_tick_value', 'trade_contract_size', 'currency_profit', 'swap_mode',
                'swap_long', 'swap_short', 'swap_rollover3days', 'volume_min', 'volume_step')
        (directory / 'symbol.json').write_text(json.dumps(
            {'server': SERVER, 'symbol': symbol, **{key: getattr(info, key) for key in keys}},
            indent=2), encoding='utf-8')
        day = options.start
        while day < end:
            start = datetime.combine(day, time.min, timezone.utc)
            stop = start + timedelta(days=1)
            for kind in options.kind:
                output = directory / kind / f'{day}.npz'
                if output.exists():
                    continue
                if kind == 'm1':
                    rows = mt.copy_rates_range(symbol, mt.TIMEFRAME_M1, start, stop)
                else:
                    rows = mt.copy_ticks_range(symbol, start, stop, mt.COPY_TICKS_INFO)
                if rows is None:
                    print(f'{symbol} {day} {kind}: {mt.last_error()}', file=sys.stderr, flush=True)
                    failed += 1
                    continue
                field, scale = ('time', 1) if kind == 'm1' else ('time_msc', 1000)
                t = rows[field]
                rows = rows[(t >= int(start.timestamp()) * scale) & (t < int(stop.timestamp()) * scale)]
                if kind == 'ticks':
                    rows = rows[(rows['bid'] > 0) & (rows['ask'] >= rows['bid'])]
                if not len(rows) or np.any(np.diff(rows[field]) < (1 if kind == 'm1' else 0)):
                    print(f'{symbol} {day} {kind}: empty or unordered', file=sys.stderr, flush=True)
                    failed += 1
                    continue
                save_day(output, rows)
                print(f'{symbol} {day} {kind}: {len(rows):,} rows', flush=True)
            day += timedelta(days=1)
    return int(failed != 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--terminal', required=True, help='Dedicated portable MT5 terminal64.exe path')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--start', type=date.fromisoformat, required=True)
    parser.add_argument('--end', type=date.fromisoformat, required=True, help='Exclusive UTC date')
    parser.add_argument('--symbols', nargs='+', choices=SYMBOLS, default=SYMBOLS)
    parser.add_argument('--kind', nargs='+', choices=('m1', 'ticks'), default=('m1', 'ticks'))
    options = parser.parse_args()
    import MetaTrader5 as mt

    if not mt.initialize(path=options.terminal, portable=True, timeout=10_000):
        raise SystemExit(f'MT5 initialization failed: {mt.last_error()}')
    try:
        return export(mt, options)
    finally:
        mt.shutdown()


if __name__ == '__main__':
    sys.exit(main())
