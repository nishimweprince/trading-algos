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
                    rr_color = "#28a745" if rr_ratio >= 2.0 else "#ffc107" if rr_ratio >= 1.5 else "#dc3545"
                    risk_reward_html = f"""
                    <div style="background: #f8f9fa; padding: 10px; border-radius: 6px; margin-top: 10px; text-align: center;">
                        <div style="font-size: 12px; color: #6c757d; margin-bottom: 4px;">Risk/Reward Ratio</div>
                        <div style="font-size: 24px; font-weight: bold; color: {rr_color};">{rr_ratio:.2f}:1</div>
                    </div>
                    """
            
            # Build stop loss / take profit HTML with better visual design
            sl_tp_html = ""
            if stop_loss or take_profit:
                sl_tp_html = '<div style="margin-top: 20px; background: #f8f9fa; padding: 15px; border-radius: 8px; border-left: 4px solid #007bff;">'
                sl_tp_html += '<div style="font-weight: bold; color: #495057; margin-bottom: 12px; font-size: 14px; text-transform: uppercase; letter-spacing: 0.5px;">Risk Management</div>'
                sl_tp_html += '<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px;">'
                if stop_loss:
                    sl_tp_html += f'''
                    <div>
                        <div style="font-size: 11px; color: #6c757d; margin-bottom: 4px; text-transform: uppercase;">Stop Loss</div>
                        <div style="font-size: 18px; font-weight: bold; color: #dc3545;">{stop_loss:.5f}</div>
                    </div>
                    '''
                if take_profit:
                    sl_tp_html += f'''
                    <div>
                        <div style="font-size: 11px; color: #6c757d; margin-bottom: 4px; text-transform: uppercase;">Take Profit</div>
                        <div style="font-size: 18px; font-weight: bold; color: #28a745;">{take_profit:.5f}</div>
                    </div>
                    '''
                sl_tp_html += '</div>'
                sl_tp_html += risk_reward_html
                sl_tp_html += '</div>'
            
            # Build reasons HTML with better styling
            reasons_html = ""
            if reasons:
                reasons_list = "".join([
                    f'<li style="margin: 8px 0; padding-left: 8px; line-height: 1.5;">{r}</li>' 
                    for r in reasons
                ])
                reasons_html = f"""
                <div style="margin-top: 20px; background: #fff; padding: 15px; border-radius: 8px; border-left: 4px solid #17a2b8;">
                    <div style="font-weight: bold; color: #495057; margin-bottom: 12px; font-size: 14px; text-transform: uppercase; letter-spacing: 0.5px;">Signal Analysis</div>
                    <ul style="margin: 0; padding-left: 20px; color: #495057; list-style: none;">
                        {reasons_list}
                    </ul>
                </div>
                """
            
            # Calculate strength color and visual indicator
            strength_color = "#28a745" if strength >= 0.7 else "#ffc107" if strength >= 0.5 else "#dc3545"
            strength_width = int(strength * 100)
            
            # Convert hex color to rgba for background with opacity
            hex_color = signal_color.lstrip('#')
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            signal_bg_start = f"rgba({r}, {g}, {b}, 0.15)"
            signal_bg_end = f"rgba({r}, {g}, {b}, 0.05)"
            signal_border = f"rgba({r}, {g}, {b}, 0.4)"
            signal_shadow = f"rgba({r}, {g}, {b}, 0.4)"

            # HTML email body
            html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
            </head>
            <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 0; background-color: #f5f5f5;">
                <!-- Header -->
                <div style="background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); color: white; padding: 25px 30px; border-radius: 10px 10px 0 0;">
                    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
                        <h1 style="margin: 0; font-size: 22px; font-weight: 600;">{signal_emoji} Trading Signal Alert</h1>
                    </div>
                    <p style="margin: 0; opacity: 0.85; font-size: 13px;">{timestamp}</p>
                </div>
                
                <!-- Main Content -->
                <div style="background: white; padding: 0; border: 1px solid #e0e0e0; border-top: none;">
                    <!-- Signal Header Card -->
                    <div style="background: linear-gradient(135deg, {signal_bg_start} 0%, {signal_bg_end} 100%); padding: 25px 30px; border-bottom: 2px solid {signal_border};">
                        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px;">
                            <div>
                                <div style="font-size: 11px; color: #6c757d; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 1px; font-weight: 600;">Signal Type</div>
                                <span style="background: {signal_color}; color: white; padding: 10px 24px; border-radius: 6px; font-weight: bold; font-size: 20px; display: inline-block; box-shadow: 0 2px 8px {signal_shadow};">
                                    {direction}
                                </span>
                            </div>
                            <div style="text-align: right;">
                                <div style="font-size: 11px; color: #6c757d; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 1px; font-weight: 600;">Instrument</div>
                                <div style="font-size: 28px; font-weight: bold; color: #212529; letter-spacing: 1px;">{instrument.replace('_', '/')}</div>
                            </div>
                        </div>
                        
                        <!-- Price - Most Prominent -->
                        <div style="background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); margin-bottom: 15px;">
                            <div style="font-size: 11px; color: #6c757d; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px; font-weight: 600;">Entry Price</div>
                            <div style="font-size: 36px; font-weight: bold; color: #212529; letter-spacing: -0.5px;">{price:.5f}</div>
                        </div>
                        
                        <!-- Signal Strength with Visual Bar -->
                        <div>
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                                <div style="font-size: 12px; color: #6c757d; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600;">Signal Strength</div>
                                <div style="font-size: 16px; font-weight: bold; color: {strength_color};">{strength:.0%}</div>
                            </div>
                            <div style="background: #e9ecef; height: 8px; border-radius: 4px; overflow: hidden;">
                                <div style="background: {strength_color}; height: 100%; width: {strength_width}%; border-radius: 4px; transition: width 0.3s ease;"></div>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Risk Management Section -->
                    {sl_tp_html}
                    
                    <!-- Signal Analysis Section -->
                    {reasons_html}
                </div>
                
                <!-- Footer -->
                <div style="background: #1a1a2e; color: white; padding: 20px 30px; border-radius: 0 0 10px 10px; text-align: center;">
                    <p style="margin: 0; font-size: 13px; font-weight: 500;">VRVP Trading Strategy</p>
                    <p style="margin: 8px 0 0 0; opacity: 0.7; font-size: 11px;">This is an automated signal notification. Trade responsibly.</p>
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
