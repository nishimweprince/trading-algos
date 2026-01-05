"""
VRVP Strategy Configuration Settings
"""
import os
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

load_dotenv()

@dataclass
class SupertrendConfig:
    period: int = 10
    multiplier: float = 3.0
    source: str = 'hl2'

@dataclass
class StochRSIConfig:
    rsi_period: int = 14
    stoch_period: int = 14
    k_smooth: int = 3
    d_smooth: int = 3
    oversold: float = 20.0
    overbought: float = 80.0

@dataclass
class VolumeProfileConfig:
    lookback_periods: int = 100
    num_bins: int = 50
    value_area_pct: float = 0.70
    proximity_atr_mult: float = 1.0

@dataclass
class FVGConfig:
    max_zones: int = 20
    min_gap_atr_mult: float = 0.1

@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 2.0
    max_position_pct: float = 10.0
    max_drawdown_pct: float = 15.0
    stop_loss_atr_mult: float = 2.0
    take_profit_atr_mult: float = 4.0
    min_risk_reward: float = 1.5
    breakeven_trigger_pct: float = 1.0

@dataclass
class TradingConfig:
    instruments: List[str] = field(default_factory=lambda: ['EUR_USD'])
    timeframe: str = '1H'
    htf_timeframe: str = '4H'
    min_candles_between_trades: int = 2
    trading_hours_start: int = 0
    trading_hours_end: int = 24

@dataclass
class OANDAConfig:
    api_token: str = field(default_factory=lambda: os.getenv('OANDA_API_TOKEN', ''))
    account_id: str = field(default_factory=lambda: os.getenv('OANDA_ACCOUNT_ID', ''))
    environment: str = field(default_factory=lambda: os.getenv('OANDA_ENVIRONMENT', 'practice'))

    @property
    def api_url(self) -> str:
        if self.environment == 'live':
            return 'https://api-fxtrade.oanda.com'
        return 'https://api-fxpractice.oanda.com'

@dataclass
class BacktestConfig:
    initial_capital: float = 10000.0
    commission_pct: float = 0.0
    spread_pips: float = 1.5
    data_path: str = 'data/historical'

@dataclass
class LoggingConfig:
    level: str = 'INFO'
    log_file: str = 'logs/vrvp_strategy.log'
    log_trades: bool = True
    log_signals: bool = True

@dataclass
class StrategyConfig:
    supertrend: SupertrendConfig = field(default_factory=SupertrendConfig)
    stochrsi: StochRSIConfig = field(default_factory=StochRSIConfig)
    volume_profile: VolumeProfileConfig = field(default_factory=VolumeProfileConfig)
    fvg: FVGConfig = field(default_factory=FVGConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    oanda: OANDAConfig = field(default_factory=OANDAConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

def load_config() -> StrategyConfig:
    config = StrategyConfig()
    if os.getenv('RISK_PER_TRADE'):
        config.risk.risk_per_trade_pct = float(os.getenv('RISK_PER_TRADE'))
    if os.getenv('MAX_DRAWDOWN'):
        config.risk.max_drawdown_pct = float(os.getenv('MAX_DRAWDOWN'))
    if os.getenv('INSTRUMENTS'):
        config.trading.instruments = os.getenv('INSTRUMENTS').split(',')
    if os.getenv('TIMEFRAME'):
        config.trading.timeframe = os.getenv('TIMEFRAME')
    return config

DEFAULT_CONFIG = load_config()
