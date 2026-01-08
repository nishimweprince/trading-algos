from __future__ import annotations

import argparse
from dataclasses import asdict

import backtrader as bt

from forex_mtf_strategy.config.settings import BacktestParams, StrategyParams
from forex_mtf_strategy.data.loader import load_ohlcv_csv
from forex_mtf_strategy.backtest.feeds import SignalPandasData
from forex_mtf_strategy.backtest.strategy import SignalStrategy
from forex_mtf_strategy.indicators.volume_profile import pip_size_for_symbol
from forex_mtf_strategy.strategy.signal_generator import generate_signals


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest the MTF forex strategy.")
    p.add_argument("--csv", required=True, help="Path to 1H OHLCV CSV")
    p.add_argument("--symbol", default="EURUSD", help="Symbol used for pip sizing")
    p.add_argument("--cash", type=float, default=BacktestParams.cash)
    p.add_argument("--risk-pct", type=float, default=BacktestParams.risk_pct)
    p.add_argument("--spread-pips", type=float, default=BacktestParams.spread_pips)
    p.add_argument("--commission-pct", type=float, default=BacktestParams.commission_pct)
    return p.parse_args()


def main() -> None:
    args = parse_args()

    df = load_ohlcv_csv(args.csv)
    params = StrategyParams()
    df_sig = generate_signals(df, symbol=args.symbol, params=params)

    # Drop warmup rows with missing pieces
    df_sig = df_sig.dropna(subset=["atr", "trend_4h"])
    df_sig["buy_signal"] = df_sig["buy_signal"].astype(bool)
    df_sig["sell_signal"] = df_sig["sell_signal"].astype(bool)

    cerebro = bt.Cerebro(runonce=False, stdstats=False)
    cerebro.broker.setcash(float(args.cash))
    cerebro.broker.setcommission(commission=float(args.commission_pct))

    data = SignalPandasData(dataname=df_sig)
    cerebro.adddata(data)

    pip_size = pip_size_for_symbol(args.symbol)
    cerebro.addstrategy(
        SignalStrategy,
        risk_pct=float(args.risk_pct),
        spread_pips=float(args.spread_pips),
        pip_size=float(pip_size),
        sl_atr_mult=float(params.sl_atr_mult),
        tp_rr=float(params.tp_rr),
    )

    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Days)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

    start = cerebro.broker.getvalue()
    results = cerebro.run()
    strat = results[0]
    end = cerebro.broker.getvalue()

    sharpe = strat.analyzers.sharpe.get_analysis()
    dd = strat.analyzers.dd.get_analysis()
    trades = strat.analyzers.trades.get_analysis()

    print("=== Backtest results ===")
    print(f"Start value: {start:,.2f}")
    print(f"End value:   {end:,.2f}")
    print(f"Net PnL:     {end - start:,.2f}")
    print(f"Sharpe:      {sharpe.get('sharperatio')}")
    print(f"Max DD:      {dd.get('max', {}).get('drawdown')}")
    print(f"Trades:      {trades.get('total', {}).get('total')}")

    print("\n=== Strategy parameters ===")
    print(asdict(params))


if __name__ == "__main__":
    main()

