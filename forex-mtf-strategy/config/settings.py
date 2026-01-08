"""
Settings configuration for the Forex MTF Strategy.

Loads configuration from environment variables and YAML files.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


@dataclass
class OANDAConfig:
    """OANDA API configuration."""
    
    access_token: str = field(default_factory=lambda: os.getenv("OANDA_ACCESS_TOKEN", ""))
    account_id: str = field(default_factory=lambda: os.getenv("OANDA_ACCOUNT_ID", ""))
    environment: str = field(default_factory=lambda: os.getenv("OANDA_ENVIRONMENT", "practice"))
    
    @property
    def api_url(self) -> str:
        """Get the API URL based on environment."""
        if self.environment == "live":
            return "https://api-fxtrade.oanda.com"
        return "https://api-fxpractice.oanda.com"
    
    @property
    def stream_url(self) -> str:
        """Get the streaming URL based on environment."""
        if self.environment == "live":
            return "https://stream-fxtrade.oanda.com"
        return "https://stream-fxpractice.oanda.com"


@dataclass
class SupertrendParams:
    """Supertrend indicator parameters."""
    
    length: int = 10
    multiplier: float = 3.0


@dataclass
class StochRSIParams:
    """Stochastic RSI indicator parameters."""
    
    length: int = 14
    rsi_length: int = 14
    k: int = 3
    d: int = 3
    oversold: float = 30.0
    overbought: float = 70.0


@dataclass
class VolumeProfileParams:
    """Volume Profile indicator parameters."""
    
    num_bins: int = 50
    value_area_pct: float = 0.70  # 70% of volume for value area
    hvn_std_multiplier: float = 1.0  # HVN threshold: mean + std * multiplier


@dataclass
class RiskParams:
    """Risk management parameters."""
    
    max_risk_per_trade: float = 0.02  # 2% per trade
    max_total_exposure: float = 0.06  # 6% total
    default_pip_value: float = 10.0  # For standard lot
    max_position_size: float = 100000.0  # 1 lot cap


@dataclass
class StrategyParams:
    """Strategy-specific parameters."""
    
    primary_timeframe: str = "H1"  # 1 hour
    trend_timeframe: str = "H4"  # 4 hours
    supertrend: SupertrendParams = field(default_factory=SupertrendParams)
    stochrsi: StochRSIParams = field(default_factory=StochRSIParams)
    volume_profile: VolumeProfileParams = field(default_factory=VolumeProfileParams)
    fvg_min_gap_pips: float = 5.0  # Minimum FVG size in pips
    lookback_periods: int = 100  # How far back to look for FVGs


@dataclass
class Settings:
    """Main settings container."""
    
    oanda: OANDAConfig = field(default_factory=OANDAConfig)
    strategy: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskParams = field(default_factory=RiskParams)
    
    # Paths
    base_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent)
    data_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent / "data" / "historical")
    logs_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent / "logs")
    
    # Logging
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    
    def __post_init__(self):
        """Create necessary directories."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
    
    @classmethod
    def from_yaml(cls, yaml_path: Optional[str] = None) -> "Settings":
        """Load settings from YAML file with environment variable overrides."""
        settings = cls()
        
        if yaml_path and Path(yaml_path).exists():
            with open(yaml_path, "r") as f:
                config = yaml.safe_load(f)
            
            # Apply YAML overrides
            if "strategy" in config:
                strategy_cfg = config["strategy"]
                if "supertrend" in strategy_cfg:
                    settings.strategy.supertrend = SupertrendParams(**strategy_cfg["supertrend"])
                if "stochrsi" in strategy_cfg:
                    settings.strategy.stochrsi = StochRSIParams(**strategy_cfg["stochrsi"])
                if "volume_profile" in strategy_cfg:
                    settings.strategy.volume_profile = VolumeProfileParams(**strategy_cfg["volume_profile"])
            
            if "risk" in config:
                settings.risk = RiskParams(**config["risk"])
        
        return settings


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create the global settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
