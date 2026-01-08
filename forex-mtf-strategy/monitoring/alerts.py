"""
Alert system for trade notifications.

Supports console, file, and Telegram notifications.
"""

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests

from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Alert:
    """Alert message container."""
    
    title: str
    message: str
    level: str = "INFO"  # INFO, WARNING, ERROR, CRITICAL
    timestamp: datetime = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()
    
    def format(self) -> str:
        """Format alert as string."""
        return f"[{self.level}] {self.title}\n{self.message}"
    
    def format_markdown(self) -> str:
        """Format alert as Markdown."""
        emoji = {
            "INFO": "ℹ️",
            "WARNING": "⚠️",
            "ERROR": "❌",
            "CRITICAL": "🚨",
            "SIGNAL": "📊",
            "TRADE": "💰",
        }.get(self.level, "📌")
        
        return f"{emoji} **{self.title}**\n\n{self.message}"


class AlertChannel(ABC):
    """Abstract base class for alert channels."""
    
    @abstractmethod
    def send(self, alert: Alert) -> bool:
        """
        Send an alert.
        
        Args:
            alert: Alert to send
            
        Returns:
            True if sent successfully
        """
        pass


class ConsoleAlertChannel(AlertChannel):
    """Console alert channel (prints to stdout)."""
    
    def send(self, alert: Alert) -> bool:
        """Print alert to console."""
        print(f"\n{'='*50}")
        print(f"[{alert.timestamp:%Y-%m-%d %H:%M:%S}] {alert.level}")
        print(f"{alert.title}")
        print(f"-"*50)
        print(alert.message)
        print(f"{'='*50}\n")
        return True


class TelegramAlertChannel(AlertChannel):
    """Telegram alert channel."""
    
    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ):
        """
        Initialize Telegram channel.
        
        Args:
            bot_token: Telegram bot token
            chat_id: Telegram chat ID
        """
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}"
    
    @property
    def is_configured(self) -> bool:
        """Check if Telegram is configured."""
        return bool(self.bot_token and self.chat_id)
    
    def send(self, alert: Alert) -> bool:
        """Send alert via Telegram."""
        if not self.is_configured:
            logger.warning("Telegram not configured, skipping alert")
            return False
        
        try:
            response = requests.post(
                f"{self.api_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": alert.format_markdown(),
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
            
            if response.status_code == 200:
                logger.debug("Telegram alert sent successfully")
                return True
            else:
                logger.error(f"Telegram error: {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False


class AlertManager:
    """
    Manage alert channels and send notifications.
    
    Aggregates multiple channels and handles alert routing.
    """
    
    def __init__(self):
        """Initialize alert manager with default channels."""
        self.channels: list[AlertChannel] = []
        
        # Always add console
        self.channels.append(ConsoleAlertChannel())
        
        # Add Telegram if configured
        telegram = TelegramAlertChannel()
        if telegram.is_configured:
            self.channels.append(telegram)
            logger.info("Telegram alerts enabled")
    
    def add_channel(self, channel: AlertChannel):
        """Add an alert channel."""
        self.channels.append(channel)
    
    def send(self, alert: Alert):
        """
        Send alert to all channels.
        
        Args:
            alert: Alert to send
        """
        for channel in self.channels:
            try:
                channel.send(alert)
            except Exception as e:
                logger.error(f"Alert channel error: {e}")
    
    def signal_alert(
        self,
        instrument: str,
        signal_type: str,
        price: float,
        strength: int,
        details: str = "",
    ):
        """
        Send a signal alert.
        
        Args:
            instrument: Instrument name
            signal_type: BUY or SELL
            price: Signal price
            strength: Signal strength
            details: Additional details
        """
        alert = Alert(
            title=f"Signal: {signal_type} {instrument}",
            message=(
                f"Price: {price:.5f}\n"
                f"Strength: {'⭐' * strength}\n"
                f"{details}"
            ),
            level="SIGNAL",
        )
        self.send(alert)
    
    def trade_alert(
        self,
        instrument: str,
        action: str,  # OPENED, CLOSED, MODIFIED
        side: str,
        units: int,
        price: float,
        pnl: Optional[float] = None,
    ):
        """
        Send a trade alert.
        
        Args:
            instrument: Instrument name
            action: Trade action
            side: BUY or SELL
            units: Trade size
            price: Trade price
            pnl: P&L if closing
        """
        pnl_str = ""
        if pnl is not None:
            pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            pnl_str = f"\nP&L: {pnl_emoji} ${pnl:.2f}"
        
        alert = Alert(
            title=f"Trade {action}: {side} {instrument}",
            message=(
                f"Size: {units:,} units\n"
                f"Price: {price:.5f}"
                f"{pnl_str}"
            ),
            level="TRADE",
        )
        self.send(alert)
    
    def error_alert(self, title: str, message: str):
        """
        Send an error alert.
        
        Args:
            title: Error title
            message: Error message
        """
        alert = Alert(
            title=title,
            message=message,
            level="ERROR",
        )
        self.send(alert)
    
    def daily_summary(
        self,
        date: datetime,
        trades: int,
        wins: int,
        losses: int,
        pnl: float,
        balance: float,
    ):
        """
        Send daily trading summary.
        
        Args:
            date: Summary date
            trades: Number of trades
            wins: Winning trades
            losses: Losing trades
            pnl: Daily P&L
            balance: Current balance
        """
        win_rate = wins / trades * 100 if trades > 0 else 0
        
        alert = Alert(
            title=f"Daily Summary - {date:%Y-%m-%d}",
            message=(
                f"📊 Trades: {trades}\n"
                f"✅ Wins: {wins} | ❌ Losses: {losses}\n"
                f"📈 Win Rate: {win_rate:.1f}%\n"
                f"💰 P&L: ${pnl:+.2f}\n"
                f"💼 Balance: ${balance:,.2f}"
            ),
            level="INFO",
        )
        self.send(alert)
