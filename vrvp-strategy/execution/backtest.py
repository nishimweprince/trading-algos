"""Backtest Engine"""
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import List
from datetime import datetime
from loguru import logger

from ..strategy import SignalGenerator
from ..risk import PositionSizer, StopManager
from ..config import StrategyConfig, DEFAULT_CONFIG

@dataclass
class Trade:
    entry_time: datetime
    exit_time: datetime
    instrument: str
    direction: int
    units: int
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    exit_reason: str

@dataclass
class BacktestResult:
    initial_balance: float
    final_balance: float
    total_return_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe_ratio: float
    trades: List[Trade]
    equity_curve: pd.Series

class BacktestEngine:
    def __init__(self, config: StrategyConfig = None):
        self.config = config or DEFAULT_CONFIG
        self.signal_gen = SignalGenerator(self.config)
        self.position_sizer = PositionSizer(self.config.risk)
        self.stop_manager = StopManager(self.config.risk)

    def run(self, df: pd.DataFrame, htf_df: pd.DataFrame = None, instrument: str = 'EUR_USD', initial_balance: float = None) -> BacktestResult:
        if initial_balance is None:
            initial_balance = self.config.backtest.initial_capital

        df_signals = self.signal_gen.generate_signals(df, htf_df, instrument)
        trades, equity = [], [initial_balance]
        balance, position, peak_balance, max_drawdown = initial_balance, None, initial_balance, 0.0

        for i in range(1, len(df_signals)):
            row = df_signals.iloc[i]
            signal = row['signal']

            if position:
                exit_reason, exit_price = None, None
                if position['direction'] == 1:
                    if row['low'] <= position['stop_loss']: exit_price, exit_reason = position['stop_loss'], 'stop_loss'
                    elif row['high'] >= position['take_profit']: exit_price, exit_reason = position['take_profit'], 'take_profit'
                else:
                    if row['high'] >= position['stop_loss']: exit_price, exit_reason = position['stop_loss'], 'stop_loss'
                    elif row['low'] <= position['take_profit']: exit_price, exit_reason = position['take_profit'], 'take_profit'

                if not exit_reason and ((position['direction'] == 1 and signal in [-1, -2]) or (position['direction'] == -1 and signal in [1, 2])):
                    exit_price, exit_reason = row['close'], 'signal_exit'

                if exit_reason:
                    pnl = (exit_price - position['entry_price']) * position['units'] * position['direction']
                    pnl -= self.config.backtest.spread_pips * 0.0001 * position['units']
                    trades.append(Trade(entry_time=position['entry_time'], exit_time=row.name, instrument=instrument,
                                        direction=position['direction'], units=position['units'], entry_price=position['entry_price'],
                                        exit_price=exit_price, pnl=pnl, pnl_pct=pnl / balance * 100, exit_reason=exit_reason))
                    balance += pnl
                    position = None

            if position is None and signal in [1, -1]:
                direction = 1 if signal == 1 else -1
                entry_price = row['close']
                atr = row.get('atr', row['high'] - row['low'])
                stops = self.stop_manager.calculate_stops(entry_price, atr, direction)
                pos_size = self.position_sizer.calculate_position_size(balance, entry_price, stops.stop_loss, instrument)
                if pos_size.units > 0:
                    position = {'direction': direction, 'units': pos_size.units, 'entry_price': entry_price,
                                'entry_time': row.name, 'stop_loss': stops.stop_loss, 'take_profit': stops.take_profit}

            equity.append(balance)
            if balance > peak_balance: peak_balance = balance
            max_drawdown = max(max_drawdown, (peak_balance - balance) / peak_balance * 100)

        return self._calculate_metrics(initial_balance, balance, trades, equity, max_drawdown)

    def _calculate_metrics(self, initial_balance, final_balance, trades, equity, max_drawdown) -> BacktestResult:
        total_return_pct = (final_balance - initial_balance) / initial_balance * 100
        if not trades:
            return BacktestResult(initial_balance=initial_balance, final_balance=final_balance, total_return_pct=total_return_pct,
                                  total_trades=0, winning_trades=0, losing_trades=0, win_rate=0, avg_win=0, avg_loss=0,
                                  profit_factor=0, max_drawdown_pct=max_drawdown, sharpe_ratio=0, trades=[], equity_curve=pd.Series(equity))

        winning, losing = [t for t in trades if t.pnl > 0], [t for t in trades if t.pnl <= 0]
        gross_profit, gross_loss = sum(t.pnl for t in winning), abs(sum(t.pnl for t in losing))
        returns = pd.Series([t.pnl_pct for t in trades])

        return BacktestResult(
            initial_balance=initial_balance, final_balance=final_balance, total_return_pct=total_return_pct,
            total_trades=len(trades), winning_trades=len(winning), losing_trades=len(losing),
            win_rate=len(winning) / len(trades) * 100, avg_win=np.mean([t.pnl for t in winning]) if winning else 0,
            avg_loss=np.mean([abs(t.pnl) for t in losing]) if losing else 0,
            profit_factor=gross_profit / gross_loss if gross_loss > 0 else 0, max_drawdown_pct=max_drawdown,
            sharpe_ratio=returns.mean() / returns.std() * np.sqrt(252) if returns.std() > 0 else 0,
            trades=trades, equity_curve=pd.Series(equity))
