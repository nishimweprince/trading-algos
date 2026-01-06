"""Logging Configuration"""
import sys
from pathlib import Path
from loguru import logger
from config import LoggingConfig

def setup_logging(config: LoggingConfig = None):
    if config is None: config = LoggingConfig()
    logger.remove()
    logger.add(sys.stderr, level=config.level, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>")
    Path(config.log_file).parent.mkdir(parents=True, exist_ok=True)
    logger.add(config.log_file, level=config.level, rotation="1 day", retention="30 days", compression="gz")

def log_signal(instrument: str, signal_type: str, price: float, reasons: list, strength: float = 0):
    logger.info(f"SIGNAL | {instrument} | {signal_type} | Price: {price:.5f} | Strength: {strength:.2f} | {', '.join(reasons)}")

def log_trade(action: str, instrument: str, units: int, price: float, stop_loss: float = None, take_profit: float = None, pnl: float = None, trade_id: str = None):
    msg = f"TRADE | {action} | {instrument} | {'LONG' if units > 0 else 'SHORT'} | {abs(units)} units @ {price:.5f}"
    if stop_loss: msg += f" | SL: {stop_loss:.5f}"
    if take_profit: msg += f" | TP: {take_profit:.5f}"
    if pnl is not None: msg += f" | PnL: ${pnl:.2f}"
    if trade_id: msg += f" | ID: {trade_id}"
    logger.info(msg)
