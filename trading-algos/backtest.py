import backtrader as bt
import pandas as pd
import datetime
from data.historical import generate_dummy_data
from indicators.bt_supertrend import SuperTrend
from config import settings

# Generate dummy data for backtesting
print("Generating dummy data...")
# Ensure we have enough data and variation
df = generate_dummy_data(days=60, timeframe='1H') 
df.to_csv('dummy_data.csv')

class StochRSI(bt.Indicator):
    lines = ('k', 'd')
    params = (
        ('period', 14), # RSI Period
        ('stoch_period', 14),
        ('period_k', 3),
        ('period_d', 3),
    )

    def __init__(self):
        rsi = bt.indicators.RSI(self.data, period=self.params.period)
        
        highest_rsi = bt.indicators.Highest(rsi, period=self.params.stoch_period)
        lowest_rsi = bt.indicators.Lowest(rsi, period=self.params.stoch_period)
        
        # Safe Division Logic
        numerator = rsi - lowest_rsi
        denominator = highest_rsi - lowest_rsi
        
        # Ensure denominator is at least a small positive number
        # This handles 0 and ensures no division by zero
        safe_denom = bt.Max(denominator, 1.0e-5)
        
        stoch_rsi = 100.0 * (numerator / safe_denom)
        
        self.lines.k = bt.indicators.SMA(stoch_rsi, period=self.params.period_k)
        self.lines.d = bt.indicators.SMA(self.lines.k, period=self.params.period_d)

class ForexStrategy(bt.Strategy):
    params = (
        ('risk_pct', 0.02),
    )

    def __init__(self):
        # Data0 is 1H (base)
        # Data1 is 4H (resampled)
        
        print(f"Initializing Strategy with k={settings.STOCHRSI_K}, d={settings.STOCHRSI_D}")
        
        # Indicators
        # 1H StochRSI
        self.stochrsi = StochRSI(self.data0, 
                                 period=settings.STOCHRSI_RSI_LENGTH, 
                                 stoch_period=settings.STOCHRSI_LENGTH,
                                 period_k=settings.STOCHRSI_K,
                                 period_d=settings.STOCHRSI_D)
        
        # 4H SuperTrend
        # We apply it to data1
        self.supertrend_4h = SuperTrend(self.data1, 
                                        period=settings.SUPERTREND_LENGTH, 
                                        multiplier=settings.SUPERTREND_MULTIPLIER)
        
    def next(self):
        # Trading Logic
        
        # Check if we have enough data
        if len(self.data0) < 50 or len(self.data1) < 50:
            return

        # 4H Trend Direction
        trend_direction = self.supertrend_4h.direction[0]
        
        # 1H Momentum
        k = self.stochrsi.k[0]
        prev_k = self.stochrsi.k[-1]
        
        # Buy Signal
        if not self.position:
            if trend_direction == 1: # Bullish Trend
                if prev_k < settings.STOCHRSI_OVERSOLD and k >= settings.STOCHRSI_OVERSOLD:
                    # Buy
                    price = self.data0.close[0]
                    # Simple Sizing
                    cash = self.broker.get_cash()
                    size = (cash * self.params.risk_pct) / price
                    # Ensure size is valid (e.g. min 1 unit)
                    if size < 1: size = 1
                    self.buy(size=size)
                    
        # Sell Signal (Exit)
        elif self.position.size > 0:
            if trend_direction == -1: # Trend reversed
                self.close()
            elif k > settings.STOCHRSI_OVERBOUGHT:
                self.close()

def run_backtest():
    cerebro = bt.Cerebro()
    
    # Load Data
    data = bt.feeds.GenericCSVData(
        dataname='dummy_data.csv',
        dtformat='%Y-%m-%d %H:%M:%S',
        timeframe=bt.TimeFrame.Minutes,
        compression=60,
        openinterest=-1
    )
    
    cerebro.adddata(data)
    
    # Resample to 4H
    cerebro.resampledata(data, timeframe=bt.TimeFrame.Minutes, compression=240)
    
    # Add Strategy
    cerebro.addstrategy(ForexStrategy)
    
    # Broker
    cerebro.broker.setcash(10000.0)
    cerebro.broker.setcommission(commission=0.0001) # Approx forex spread/comm
    
    print(f'Starting Portfolio Value: {cerebro.broker.getvalue():.2f}')
    cerebro.run()
    print(f'Final Portfolio Value: {cerebro.broker.getvalue():.2f}')

if __name__ == '__main__':
    run_backtest()
