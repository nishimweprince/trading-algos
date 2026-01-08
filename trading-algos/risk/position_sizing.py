class RiskManager:
    def __init__(self, account_balance, max_risk_pct=0.02):
        self.balance = account_balance
        self.max_risk = max_risk_pct
    
    def calculate_position_size(self, entry_price, stop_loss_price, pip_value=10): # Standard lot pip value approx $10
        """
        Calculates position size based on risk percentage and stop loss distance.
        """
        if entry_price == stop_loss_price:
            return 0
            
        risk_amount = self.balance * self.max_risk
        
        # Calculate distance in pips (approximate for non-JPY pairs)
        # 1 pip = 0.0001 usually
        price_diff = abs(entry_price - stop_loss_price)
        
        # This formula depends on the currency pair and account currency
        # Simplified: Size = Risk / (Distance * ValuePerUnit)
        
        # Assuming standard forex lot logic roughly:
        # Risk = Units * PriceDiff
        # Units = Risk / PriceDiff
        
        units = risk_amount / price_diff
        
        # Cap at reasonable limits (e.g. margin requirements) - simplified
        return int(units)

    def check_exposure(self, current_exposure_pct):
        return current_exposure_pct < 0.06
