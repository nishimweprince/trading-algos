import backtrader as bt

class SuperTrend(bt.Indicator):
    """
    SuperTrend indicator for Backtrader.
    """
    lines = ('supertrend', 'direction')
    params = (('period', 10), ('multiplier', 3.0))

    def __init__(self):
        atr = bt.indicators.ATR(self.data, period=self.params.period)
        hl2 = (self.data.high + self.data.low) / 2
        
        self.basic_upper = hl2 + (self.params.multiplier * atr)
        self.basic_lower = hl2 - (self.params.multiplier * atr)
        
        # We maintain state in python attributes
        self.final_upper_val = 0.0
        self.final_lower_val = 0.0

    def next(self):
        # Current index
        if len(self) == 1:
            self.lines.supertrend[0] = self.basic_upper[0]
            self.lines.direction[0] = 1
            self.final_upper_val = self.basic_upper[0]
            self.final_lower_val = self.basic_lower[0]
            return

        # Previous values
        prev_close = self.data.close[-1]
        prev_final_upper = self.final_upper_val
        prev_final_lower = self.final_lower_val
        
        # Calculate Final Upper
        # basic_upper[0] is current
        if (self.basic_upper[0] < prev_final_upper) or (prev_close > prev_final_upper):
            self.final_upper_val = self.basic_upper[0]
        else:
            self.final_upper_val = prev_final_upper
            
        # Calculate Final Lower
        if (self.basic_lower[0] > prev_final_lower) or (prev_close < prev_final_lower):
            self.final_lower_val = self.basic_lower[0]
        else:
            self.final_lower_val = prev_final_lower
            
        # Determine Direction and SuperTrend value
        prev_direction = self.lines.direction[-1]
        
        if prev_direction == 1: # Uptrend
            if self.data.close[0] < prev_final_lower:
                self.lines.direction[0] = -1
                self.lines.supertrend[0] = self.final_upper_val
            else:
                self.lines.direction[0] = 1
                self.lines.supertrend[0] = self.final_lower_val
        else: # Downtrend
            if self.data.close[0] > prev_final_upper:
                self.lines.direction[0] = 1
                self.lines.supertrend[0] = self.final_lower_val
            else:
                self.lines.direction[0] = -1
                self.lines.supertrend[0] = self.final_upper_val
