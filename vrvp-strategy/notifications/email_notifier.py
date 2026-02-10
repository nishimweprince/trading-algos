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
    from_email: str = "VRVP Strategy <onboarding@resend.dev>"
    enabled: bool = True

    @classmethod
    def from_env(cls) -> Optional['EmailConfig']:
        """Load email configuration from environment variables"""
        api_key = os.getenv('RESEND_API_KEY', '').strip()
        recipients_str = os.getenv('NOTIFICATION_EMAILS', '').strip()
        from_email = os.getenv('NOTIFICATION_FROM_EMAIL', '').strip() or 'VRVP Strategy <onboarding@resend.dev>'

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
            
            # Determine signal direction and palette
            if signal_type == "LONG":
                direction = "BUY"
                accent_color = "#16a34a"
                accent_soft = "#dcfce7"
            elif signal_type == "SHORT":
                direction = "SELL"
                accent_color = "#dc2626"
                accent_soft = "#fee2e2"
            else:
                direction = signal_type
                accent_color = "#0f766e"
                accent_soft = "#ccfbf1"

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
                    <div style="margin-top: 18px; padding-top: 18px; border-top: 1px dashed #cbd5e1;">
                        <div style="font-size: 11px; color: #475569; margin-bottom: 6px; letter-spacing: 0.8px; text-transform: uppercase;">Risk/Reward</div>
                        <div style="font-size: 22px; color: #0f172a; font-weight: 700;">{rr_ratio:.2f}:1</div>
                    </div>
                    """
            
            # Build stop loss / take profit HTML
            sl_tp_html = ""
            if stop_loss or take_profit:
                sl_tp_html = '<div style="margin-top: 24px; padding: 20px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 14px;">'
                sl_tp_html += '<div style="font-size: 11px; color: #475569; margin-bottom: 14px; text-transform: uppercase; letter-spacing: 1px;">Risk Management</div>'
                sl_tp_html += '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse: collapse;"><tr>'
                if stop_loss:
                    sl_tp_html += f'''
                    <td style="vertical-align: top; width: 50%; padding-right: 10px;">
                        <div style="font-size: 11px; color: #64748b; margin-bottom: 6px;">Stop Loss</div>
                        <div style="font-size: 20px; color: #0f172a; font-weight: 700;">{stop_loss:.5f}</div>
                    </td>
                    '''
                if take_profit:
                    sl_tp_html += f'''
                    <td style="vertical-align: top; width: 50%; padding-left: 10px;">
                        <div style="font-size: 11px; color: #64748b; margin-bottom: 6px;">Take Profit</div>
                        <div style="font-size: 20px; color: #0f172a; font-weight: 700;">{take_profit:.5f}</div>
                    </td>
                    '''
                sl_tp_html += '</tr></table>'
                sl_tp_html += risk_reward_html
                sl_tp_html += '</div>'
            
            # Build reasons HTML
            reasons_html = ""
            if reasons:
                reasons_list = "".join([
                    f'<li style="margin: 8px 0; line-height: 1.5; color: #334155;">{r}</li>'
                    for r in reasons
                ])
                reasons_html = f"""
                <div style="margin-top: 22px; padding: 18px 20px; border: 1px solid #e2e8f0; border-radius: 14px; background: #ffffff;">
                    <div style="font-size: 11px; color: #475569; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 1px;">Signal Analysis</div>
                    <ul style="margin: 0; padding-left: 18px; color: #334155;">
                        {reasons_list}
                    </ul>
                </div>
                """

            # Calculate strength width for progress bar
            strength_width = int(strength * 100)

            # HTML email body
            html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
            </head>
            <body style="margin: 0; padding: 0; background: #f1f5f9;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse: collapse; background: #f1f5f9; padding: 24px 0;">
                    <tr>
                        <td align="center">
                            <table role="presentation" width="620" cellpadding="0" cellspacing="0" style="max-width: 620px; width: 100%; border-collapse: collapse; background: #ffffff; border-radius: 18px; overflow: hidden; border: 1px solid #e2e8f0;">
                                <tr>
                                    <td style="padding: 26px 28px; background: linear-gradient(145deg, {accent_soft}, #ffffff); border-bottom: 1px solid #e2e8f0;">
                                        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse: collapse;">
                                            <tr>
                                                <td>
                                                    <div style="font-family: 'Trebuchet MS', Tahoma, sans-serif; font-size: 12px; color: #334155; letter-spacing: 1.1px; text-transform: uppercase; margin-bottom: 8px;">VRVP Trade Signal</div>
                                                    <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 34px; color: #0f172a; font-weight: 700; letter-spacing: 0.5px;">{instrument.replace('_', '/')}</div>
                                                    <div style="font-family: 'Trebuchet MS', Tahoma, sans-serif; font-size: 12px; color: #64748b; margin-top: 8px;">{timestamp}</div>
                                                </td>
                                                <td align="right" style="vertical-align: top;">
                                                    <div style="display: inline-block; padding: 8px 14px; background: {accent_color}; color: #ffffff; font-family: 'Trebuchet MS', Tahoma, sans-serif; font-size: 12px; letter-spacing: 1px; text-transform: uppercase; border-radius: 999px; font-weight: 700;">
                                                        {direction}
                                                    </div>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding: 26px 28px;">
                                        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse: collapse; margin-bottom: 20px;">
                                            <tr>
                                                <td style="width: 50%; padding-right: 10px; vertical-align: top;">
                                                    <div style="font-family: 'Trebuchet MS', Tahoma, sans-serif; font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.9px; margin-bottom: 7px;">Entry Price</div>
                                                    <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 34px; color: #0f172a; font-weight: 700;">{price:.5f}</div>
                                                </td>
                                                <td style="width: 50%; padding-left: 10px; vertical-align: top;">
                                                    <div style="font-family: 'Trebuchet MS', Tahoma, sans-serif; font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.9px; margin-bottom: 7px;">Signal Strength</div>
                                                    <div style="font-family: Georgia, 'Times New Roman', serif; font-size: 32px; color: #0f172a; font-weight: 700;">{strength:.0%}</div>
                                                </td>
                                            </tr>
                                        </table>
                                        <div style="margin-bottom: 22px;">
                                            <div style="background: #e2e8f0; height: 10px; border-radius: 999px; overflow: hidden;">
                                                <div style="background: {accent_color}; height: 100%; width: {strength_width}%;"></div>
                                            </div>
                                        </div>
                                        {sl_tp_html}
                                        {reasons_html}
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding: 18px 28px; border-top: 1px solid #e2e8f0; background: #f8fafc; text-align: center;">
                                        <div style="font-family: 'Trebuchet MS', Tahoma, sans-serif; font-size: 12px; color: #334155; margin-bottom: 4px;">VRVP Trading Strategy</div>
                                        <div style="font-family: 'Trebuchet MS', Tahoma, sans-serif; font-size: 11px; color: #64748b;">Automated alert only. Validate risk before execution.</div>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                </table>
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
