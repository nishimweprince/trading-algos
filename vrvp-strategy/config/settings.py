"""
VRVP Strategy Configuration Settings
"""
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv
from loguru import logger

# Load .env file from project root
project_root = Path(__file__).parent.parent.absolute()
env_path = project_root / '.env'
load_dotenv(dotenv_path=env_path)

# Log if .env file was found (for debugging)
if env_path.exists():
    logger.debug(f"Loading environment variables from: {env_path}")
else:
    logger.warning(f".env file not found at {env_path}. Using system environment variables or defaults.")

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
class CapitalComConfig:
    api_key: str = field(default_factory=lambda: os.getenv('CAPITALCOM_API_KEY', ''))
    api_password: str = field(default_factory=lambda: os.getenv('CAPITALCOM_API_PASSWORD', ''))
    username: str = field(default_factory=lambda: os.getenv('CAPITALCOM_USERNAME', ''))
    environment: str = field(default_factory=lambda: os.getenv('CAPITALCOM_ENVIRONMENT', 'demo'))

    def validate(self) -> List[str]:
        """Validate configuration and return list of issues"""
        issues = []

        if not self.api_key:
            issues.append("CAPITALCOM_API_KEY is not set")
        elif len(self.api_key) < 10:
            issues.append(f"CAPITALCOM_API_KEY seems too short ({len(self.api_key)} chars)")

        if not self.api_password:
            issues.append("CAPITALCOM_API_PASSWORD is not set")
        elif len(self.api_password) < 6:
            issues.append(f"CAPITALCOM_API_PASSWORD seems too short ({len(self.api_password)} chars)")

        if self.environment not in ['demo', 'live']:
            issues.append(f"Invalid environment: {self.environment} (must be 'demo' or 'live')")

        return issues

    @property
    def api_url(self) -> str:
        """Get base REST API URL based on environment"""
        if self.environment == 'live':
            return 'https://api-capital.backend-capital.com'
        return 'https://demo-api-capital.backend-capital.com'

    @property
    def websocket_url(self) -> str:
        """Get WebSocket streaming URL based on environment"""
        if self.environment == 'live':
            return 'wss://api-streaming-capital.backend-capital.com/connect'
        return 'wss://demo-api-streaming-capital.backend-capital.com/connect'

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
    capitalcom: CapitalComConfig = field(default_factory=CapitalComConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

def load_config() -> StrategyConfig:
    """Load configuration from environment variables and defaults."""
    config = StrategyConfig()
    
    # Load Capital.com credentials (required for live/paper trading)
    api_key = os.getenv('CAPITALCOM_API_KEY', '').strip()
    api_password = os.getenv('CAPITALCOM_API_PASSWORD', '').strip()
    username = os.getenv('CAPITALCOM_USERNAME', '').strip()
    environment = os.getenv('CAPITALCOM_ENVIRONMENT', 'demo').strip().lower()
    
    if api_key:
        config.capitalcom.api_key = api_key
    if api_password:
        config.capitalcom.api_password = api_password
    if username:
        config.capitalcom.username = username
    if environment in ['demo', 'live']:
        config.capitalcom.environment = environment
    
    # Validate required Capital.com credentials
    if not api_key or not api_password:
        logger.warning("CAPITALCOM_API_KEY and/or CAPITALCOM_API_PASSWORD not set. Live/paper trading will not work.")
    
    # Load optional risk settings
    if os.getenv('RISK_PER_TRADE'):
        try:
            config.risk.risk_per_trade_pct = float(os.getenv('RISK_PER_TRADE'))
        except (ValueError, TypeError):
            logger.warning(f"Invalid RISK_PER_TRADE value: {os.getenv('RISK_PER_TRADE')}")
    
    if os.getenv('MAX_DRAWDOWN'):
        try:
            config.risk.max_drawdown_pct = float(os.getenv('MAX_DRAWDOWN'))
        except (ValueError, TypeError):
            logger.warning(f"Invalid MAX_DRAWDOWN value: {os.getenv('MAX_DRAWDOWN')}")
    
    # Load optional trading settings
    if os.getenv('INSTRUMENTS'):
        instruments = os.getenv('INSTRUMENTS').strip()
        if instruments:
            config.trading.instruments = [i.strip() for i in instruments.split(',')]
    
    if os.getenv('TIMEFRAME'):
        timeframe = os.getenv('TIMEFRAME').strip()
        if timeframe:
            config.trading.timeframe = timeframe
    
    # Load optional logging settings
    if os.getenv('LOG_LEVEL'):
        log_level = os.getenv('LOG_LEVEL').strip().upper()
        if log_level in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
            config.logging.level = log_level

    # Validate Capital.com config
    validation_issues = config.capitalcom.validate()
    if validation_issues:
        logger.warning("Capital.com configuration issues:")
        for issue in validation_issues:
            logger.warning(f"  - {issue}")

    return config

DEFAULT_CONFIG = load_config()
