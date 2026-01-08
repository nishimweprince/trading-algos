"""Email notifications using Resend API"""
import os
import threading
from datetime import datetime
from typing import List, Optional
from dataclasses import dataclass
from loguru import logger

try:
    import resend
    RESEND_AVAILABLE = True
except ImportError:
    RESEND_AVAILABLE = False
    logger.warning("resend package not installed. Email notifications disabled.")


@dataclass
class EmailConfig:
    """Email notification configuration"""
    api_key: str
    recipients: List[str]
    from_email: str = "VRVP Strategy <signals@resend.dev>"
    enabled: bool = True

    @classmethod
    def from_env(cls) -> Optional['EmailConfig']:
        """Load email configuration from environment variables"""
        api_key = os.getenv('RESEND_API_KEY', '').strip()
        recipients_str = os.getenv('NOTIFICATION_EMAILS', '').strip()
        from_email = os.getenv('NOTIFICATION_FROM_EMAIL', 'VRVP Strategy <signals@resend.dev>').strip()

        if not api_key:
            logger.debug("RESEND_API_KEY not set. Email notifications disabled.")
            return None

        if not recipients_str:
            logger.warning("NOTIFICATION_EMAILS not set. Email notifications disabled.")
            return None

        recipients = [email.strip() for email in recipients_str.split(',') if email.strip()]
        if not recipients:
            logger.warning("No valid email addresses in NOTIFICATION_EMAILS.")
            return None

        return cls(
            api_key=api_key,
            recipients=recipients,
            from_email=from_email,
            enabled=True
        )


class EmailNotifier:
    """Sends email notifications for trading signals using Resend API"""

    def __init__(self, config: Optional[EmailConfig] = None):
        self.config = config or EmailConfig.from_env()
        self._initialized = False

        if self.config and RESEND_AVAILABLE:
            resend.api_key = self.config.api_key
            self._initialized = True
            logger.info(f"Email notifier initialized. Recipients: {self.config.recipients}")
        else:
            if not RESEND_AVAILABLE:
                logger.warning("Email notifier disabled: resend package not installed")
            elif not self.config:
                logger.info("Email notifier disabled: no configuration provided")

    @property
    def is_enabled(self) -> bool:
        """Check if email notifications are enabled and configured"""
        return self._initialized and self.config is not None and self.config.enabled

    def send_signal_notification(
        self,
        instrument: str,
        signal_type: str,
        price: float,
        strength: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        reasons: Optional[List[str]] = None
    ) -> bool:
        """
        Send email notification for a trading signal.
        Runs in a separate thread to avoid blocking.
        """
        if not self.is_enabled:
            return False

        # Run in background thread to avoid blocking
        thread = threading.Thread(
            target=self._send_signal_email,
            args=(instrument, signal_type, price, strength, stop_loss, take_profit, reasons),
            daemon=True
        )
        thread.start()
        return True

    def _send_signal_email(
        self,
        instrument: str,
        signal_type: str,
        price: float,
        strength: float,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        reasons: Optional[List[str]]
    ) -> None:
        """Internal method to send email (runs in thread)"""
        try:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')
            
            # Determine signal color/emoji
            if signal_type == "LONG":
                signal_emoji = "🟢"
                signal_color = "#28a745"
                direction = "BUY"
            elif signal_type == "SHORT":
                signal_emoji = "🔴"
                signal_color = "#dc3545"
                direction = "SELL"
            else:
                signal_emoji = "⚪"
                signal_color = "#6c757d"
                direction = signal_type

            # Build reasons HTML
            reasons_html = ""
            if reasons:
                reasons_list = "".join([f"<li>{r}</li>" for r in reasons])
                reasons_html = f"""
                <div style="margin-top: 15px;">
                    <strong>Signal Reasons:</strong>
                    <ul style="margin: 5px 0; padding-left: 20px;">
                        {reasons_list}
                    </ul>
                </div>
                """

            # Build stop loss / take profit HTML
            sl_tp_html = ""
            if stop_loss or take_profit:
                sl_tp_html = '<div style="margin-top: 15px;">'
                if stop_loss:
                    sl_tp_html += f'<p style="margin: 5px 0;"><strong>Stop Loss:</strong> {stop_loss:.5f}</p>'
                if take_profit:
                    sl_tp_html += f'<p style="margin: 5px 0;"><strong>Take Profit:</strong> {take_profit:.5f}</p>'
                sl_tp_html += '</div>'

            # HTML email body
            html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
            </head>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
                <div style="background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); color: white; padding: 20px; border-radius: 10px 10px 0 0;">
                    <h1 style="margin: 0; font-size: 24px;">{signal_emoji} Trading Signal Alert</h1>
                    <p style="margin: 5px 0 0 0; opacity: 0.9;">{timestamp}</p>
                </div>
                
                <div style="background: #f8f9fa; padding: 20px; border: 1px solid #dee2e6; border-top: none;">
                    <div style="background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                        <div style="display: flex; align-items: center; margin-bottom: 15px;">
                            <span style="background: {signal_color}; color: white; padding: 8px 16px; border-radius: 20px; font-weight: bold; font-size: 18px;">
                                {direction}
                            </span>
                            <span style="margin-left: 15px; font-size: 22px; font-weight: bold;">{instrument.replace('_', '/')}</span>
                        </div>
                        
                        <div style="border-top: 1px solid #eee; padding-top: 15px;">
                            <p style="margin: 5px 0;"><strong>Price:</strong> {price:.5f}</p>
                            <p style="margin: 5px 0;"><strong>Signal Strength:</strong> {strength:.0%}</p>
                        </div>
                        
                        {sl_tp_html}
                        {reasons_html}
                    </div>
                </div>
                
                <div style="background: #1a1a2e; color: white; padding: 15px; border-radius: 0 0 10px 10px; text-align: center; font-size: 12px;">
                    <p style="margin: 0;">VRVP Trading Strategy</p>
                    <p style="margin: 5px 0 0 0; opacity: 0.7;">This is an automated signal notification. Trade responsibly.</p>
                </div>
            </body>
            </html>
            """

            # Plain text fallback
            text_body = f"""
TRADING SIGNAL ALERT
====================
{timestamp}

{signal_emoji} {direction} {instrument.replace('_', '/')}

Price: {price:.5f}
Signal Strength: {strength:.0%}
"""
            if stop_loss:
                text_body += f"Stop Loss: {stop_loss:.5f}\n"
            if take_profit:
                text_body += f"Take Profit: {take_profit:.5f}\n"
            if reasons:
                text_body += f"\nReasons:\n" + "\n".join([f"  - {r}" for r in reasons])
            text_body += "\n\n--\nVRVP Trading Strategy"

            # Send email via Resend
            params = {
                "from": self.config.from_email,
                "to": self.config.recipients,
                "subject": f"{signal_emoji} {direction} Signal: {instrument.replace('_', '/')} @ {price:.5f}",
                "html": html_body,
                "text": text_body
            }

            response = resend.Emails.send(params)
            logger.info(f"Signal email sent successfully. ID: {response.get('id', 'unknown')}")

        except Exception as e:
            logger.error(f"Failed to send signal email: {e}")
            import traceback
            logger.debug(traceback.format_exc())


def send_signal_email(
    instrument: str,
    signal_type: str,
    price: float,
    strength: float,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    reasons: Optional[List[str]] = None,
    notifier: Optional[EmailNotifier] = None
) -> bool:
    """
    Convenience function to send a signal email.
    Creates a new notifier if not provided.
    """
    if notifier is None:
        notifier = EmailNotifier()
    
    return notifier.send_signal_notification(
        instrument=instrument,
        signal_type=signal_type,
        price=price,
        strength=strength,
        stop_loss=stop_loss,
        take_profit=take_profit,
        reasons=reasons
    )
