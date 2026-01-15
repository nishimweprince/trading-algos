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
            
            # Determine signal direction (minimalistic - no colors, just text)
            if signal_type == "LONG":
                direction = "BUY"
            elif signal_type == "SHORT":
                direction = "SELL"
            else:
                direction = signal_type

            # Calculate risk/reward ratio if both SL and TP are available
            risk_reward_html = ""
            if stop_loss and take_profit:
                if signal_type == "LONG":
                    risk = abs(price - stop_loss)
                    reward = abs(take_profit - price)
                else:  # SHORT
                    risk = abs(stop_loss - price)
                    reward = abs(price - take_profit)
                
                if risk > 0:
                    rr_ratio = reward / risk
                    risk_reward_html = f"""
                    <div style="margin-top: 20px; padding-top: 20px; border-top: 1px solid #333;">
                        <div style="font-size: 12px; color: #aaa; margin-bottom: 8px;">Risk/Reward Ratio</div>
                        <div style="font-size: 20px; color: #fff; font-weight: 400;">{rr_ratio:.2f}:1</div>
                    </div>
                    """
            
            # Build stop loss / take profit HTML - minimalistic
            sl_tp_html = ""
            if stop_loss or take_profit:
                sl_tp_html = '<div style="margin-top: 30px; padding-top: 30px; border-top: 1px solid #333;">'
                sl_tp_html += '<div style="font-size: 12px; color: #aaa; margin-bottom: 20px; text-transform: uppercase; letter-spacing: 1px;">Risk Management</div>'
                sl_tp_html += '<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 30px;">'
                if stop_loss:
                    sl_tp_html += f'''
                    <div>
                        <div style="font-size: 11px; color: #aaa; margin-bottom: 8px;">Stop Loss</div>
                        <div style="font-size: 18px; color: #fff; font-weight: 400;">{stop_loss:.5f}</div>
                    </div>
                    '''
                if take_profit:
                    sl_tp_html += f'''
                    <div>
                        <div style="font-size: 11px; color: #aaa; margin-bottom: 8px;">Take Profit</div>
                        <div style="font-size: 18px; color: #fff; font-weight: 400;">{take_profit:.5f}</div>
                    </div>
                    '''
                sl_tp_html += '</div>'
                sl_tp_html += risk_reward_html
                sl_tp_html += '</div>'
            
            # Build reasons HTML - minimalistic
            reasons_html = ""
            if reasons:
                reasons_list = "".join([
                    f'<li style="margin: 12px 0; padding-left: 0; line-height: 1.6; color: #ddd;">{r}</li>' 
                    for r in reasons
                ])
                reasons_html = f"""
                <div style="margin-top: 30px; padding-top: 30px; border-top: 1px solid #333;">
                    <div style="font-size: 12px; color: #aaa; margin-bottom: 20px; text-transform: uppercase; letter-spacing: 1px;">Signal Analysis</div>
                    <ul style="margin: 0; padding-left: 0; list-style: none; color: #ddd;">
                        {reasons_list}
                    </ul>
                </div>
                """

            # Calculate strength width for minimal progress bar
            strength_width = int(strength * 100)

            # HTML email body - minimalistic dark theme
            html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
            </head>
            <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; line-height: 1.6; color: #fff; max-width: 600px; margin: 0 auto; padding: 0; background-color: #0a0a0a;">
                <!-- Main Content -->
                <div style="background: #1a1a2e; padding: 40px 30px;">
                    <!-- Header -->
                    <div style="margin-bottom: 40px;">
                        <div style="font-size: 14px; color: #aaa; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px;">Trading Signal Alert</div>
                        <div style="font-size: 12px; color: #666; margin-top: 4px;">{timestamp}</div>
                    </div>
                    
                    <!-- Signal Type and Instrument -->
                    <div style="display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 40px; padding-bottom: 30px; border-bottom: 1px solid #333;">
                        <div>
                            <div style="font-size: 12px; color: #aaa; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px;">Signal</div>
                            <div style="font-size: 24px; color: #fff; font-weight: 400; letter-spacing: 0.5px;">{direction}</div>
                        </div>
                        <div style="text-align: right;">
                            <div style="font-size: 12px; color: #aaa; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px;">Instrument</div>
                            <div style="font-size: 24px; color: #fff; font-weight: 400; letter-spacing: 1px;">{instrument.replace('_', '/')}</div>
                        </div>
                    </div>
                    
                    <!-- Price -->
                    <div style="margin-bottom: 40px; padding-bottom: 30px; border-bottom: 1px solid #333;">
                        <div style="font-size: 12px; color: #aaa; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px;">Entry Price</div>
                        <div style="font-size: 32px; color: #fff; font-weight: 400; letter-spacing: 0.5px;">{price:.5f}</div>
                    </div>
                    
                    <!-- Signal Strength -->
                    <div style="margin-bottom: 40px; padding-bottom: 30px; border-bottom: 1px solid #333;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                            <div style="font-size: 12px; color: #aaa; text-transform: uppercase; letter-spacing: 1px;">Signal Strength</div>
                            <div style="font-size: 16px; color: #fff; font-weight: 400;">{strength:.0%}</div>
                        </div>
                        <div style="background: #2a2a3e; height: 2px; border-radius: 1px; overflow: hidden;">
                            <div style="background: #fff; height: 100%; width: {strength_width}%;"></div>
                        </div>
                    </div>
                    
                    <!-- Risk Management Section -->
                    {sl_tp_html}
                    
                    <!-- Signal Analysis Section -->
                    {reasons_html}
                </div>
                
                <!-- Footer -->
                <div style="background: #0a0a0a; color: #666; padding: 30px; text-align: center;">
                    <div style="font-size: 12px; color: #666; margin-bottom: 4px;">VRVP Trading Strategy</div>
                    <div style="font-size: 11px; color: #444; margin-top: 8px;">This is an automated signal notification. Trade responsibly.</div>
                </div>
            </body>
            </html>
            """

            # Plain text fallback
            text_body = f"""
TRADING SIGNAL ALERT
====================
{timestamp}

{direction} {instrument.replace('_', '/')}

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
                "subject": f"{direction} Signal: {instrument.replace('_', '/')} @ {price:.5f}",
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
