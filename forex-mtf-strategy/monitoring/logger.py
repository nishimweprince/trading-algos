"""
Logging configuration for the trading system.

Provides structured logging with file and console output.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Use rich for better console formatting if available
try:
    from rich.logging import RichHandler
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


# Global logger cache
_loggers: dict[str, logging.Logger] = {}
_initialized = False


def setup_logging(
    log_level: str = "INFO",
    log_dir: Optional[Path] = None,
    console: bool = True,
    file: bool = True,
) -> None:
    """
    Set up logging configuration.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_dir: Directory for log files
        console: Enable console output
        file: Enable file output
    """
    global _initialized
    
    if _initialized:
        return
    
    # Get log directory
    if log_dir is None:
        log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # Get log level
    level = getattr(logging, log_level.upper(), logging.INFO)
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Clear existing handlers
    root_logger.handlers.clear()
    
    # Console handler
    if console:
        if RICH_AVAILABLE:
            console_handler = RichHandler(
                rich_tracebacks=True,
                tracebacks_show_locals=True,
                show_time=True,
                show_path=False,
            )
            console_format = "%(message)s"
        else:
            console_handler = logging.StreamHandler(sys.stdout)
            console_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        
        console_handler.setLevel(level)
        console_handler.setFormatter(logging.Formatter(console_format, datefmt="%H:%M:%S"))
        root_logger.addHandler(console_handler)
    
    # File handler
    if file:
        log_filename = log_dir / f"trading_{datetime.now():%Y%m%d}.log"
        file_handler = logging.FileHandler(log_filename)
        file_handler.setLevel(logging.DEBUG)  # Always log everything to file
        file_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        file_handler.setFormatter(logging.Formatter(file_format))
        root_logger.addHandler(file_handler)
    
    # Reduce noise from external libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("oandapyV20").setLevel(logging.WARNING)
    
    _initialized = True
    
    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized. Level: {log_level}, Log dir: {log_dir}")


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance.
    
    Args:
        name: Logger name (usually __name__)
        
    Returns:
        Configured logger instance
    """
    global _initialized
    
    if not _initialized:
        setup_logging()
    
    if name not in _loggers:
        _loggers[name] = logging.getLogger(name)
    
    return _loggers[name]


class TradeLogger:
    """
    Specialized logger for trade events.
    
    Logs trades in a structured format for later analysis.
    """
    
    def __init__(self, log_dir: Optional[Path] = None):
        """
        Initialize trade logger.
        
        Args:
            log_dir: Directory for trade logs
        """
        self.logger = get_logger("trades")
        
        if log_dir is None:
            log_dir = Path(__file__).parent.parent / "logs"
        
        # Create separate trade log file
        trade_log = log_dir / f"trades_{datetime.now():%Y%m%d}.log"
        handler = logging.FileHandler(trade_log)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        
        # Avoid duplicate handlers
        if not any(isinstance(h, logging.FileHandler) and h.baseFilename == str(trade_log) 
                   for h in self.logger.handlers):
            self.logger.addHandler(handler)
    
    def log_signal(
        self,
        instrument: str,
        signal_type: str,
        price: float,
        strength: int,
        indicators: dict,
    ):
        """
        Log a trading signal.
        
        Args:
            instrument: Instrument name
            signal_type: BUY or SELL
            price: Signal price
            strength: Signal strength (1-4)
            indicators: Dict of indicator values
        """
        ind_str = " | ".join(f"{k}:{v}" for k, v in indicators.items())
        self.logger.info(
            f"SIGNAL | {instrument} | {signal_type} | "
            f"price:{price:.5f} | strength:{strength} | {ind_str}"
        )
    
    def log_order(
        self,
        instrument: str,
        order_type: str,
        side: str,
        units: int,
        price: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ):
        """
        Log an order placement.
        
        Args:
            instrument: Instrument name
            order_type: MARKET, LIMIT, etc.
            side: BUY or SELL
            units: Order size
            price: Order price
            stop_loss: Stop loss price
            take_profit: Take profit price
        """
        sl_str = f"SL:{stop_loss:.5f}" if stop_loss else "SL:None"
        tp_str = f"TP:{take_profit:.5f}" if take_profit else "TP:None"
        
        self.logger.info(
            f"ORDER | {instrument} | {order_type} {side} | "
            f"{units} units @ {price:.5f} | {sl_str} | {tp_str}"
        )
    
    def log_fill(
        self,
        instrument: str,
        trade_id: str,
        side: str,
        units: int,
        fill_price: float,
    ):
        """
        Log an order fill.
        
        Args:
            instrument: Instrument name
            trade_id: Trade ID
            side: BUY or SELL
            units: Filled units
            fill_price: Fill price
        """
        self.logger.info(
            f"FILL | {instrument} | {trade_id} | {side} | "
            f"{units} units @ {fill_price:.5f}"
        )
    
    def log_close(
        self,
        instrument: str,
        trade_id: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        duration_hours: float,
    ):
        """
        Log a position close.
        
        Args:
            instrument: Instrument name
            trade_id: Trade ID
            entry_price: Entry price
            exit_price: Exit price
            pnl: Realized P&L
            duration_hours: Position duration in hours
        """
        result = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"
        
        self.logger.info(
            f"CLOSE | {instrument} | {trade_id} | {result} | "
            f"entry:{entry_price:.5f} | exit:{exit_price:.5f} | "
            f"PnL:${pnl:.2f} | duration:{duration_hours:.1f}h"
        )
