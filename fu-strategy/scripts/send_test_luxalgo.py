"""Send a live test LuxAlgo Reversal notification using the real Signal + dispatcher path."""
import asyncio
from datetime import datetime, timezone

from app.config import get_settings
from app.core.types import Bias, Direction, Signal
from app.notifications.dispatcher import NotificationDispatcher
from app.notifications.log import NotificationLog
from app.notifications.sms_client import SMSClient
from app.notifications.whatsapp_client import WhatsAppClient


async def main():
    settings = get_settings()
    print("Loaded configuration settings:")
    print(f"  Channels  : {settings.notification_channels}")
    print(f"  Recipients: {settings.notification_numbers}")
    print(f"  SMS OK    : {settings.sms_configured}")
    print(f"  WA OK     : {settings.whatsapp_configured}")

    notification_log = NotificationLog(settings.notifications_log_path)
    await notification_log.init()

    whatsapp_client = WhatsAppClient(settings)
    sms_client = SMSClient(settings)
    dispatcher = NotificationDispatcher(settings, whatsapp_client, sms_client, notification_log)

    # Build a fake LuxAlgo Reversal Up signal exactly as the pipeline would produce it
    sig = Signal(
        id="TEST-LUXALGO-UP-001",
        symbol="XAUUSD",
        timeframe="1m",
        direction=Direction.BUY,
        entry_price=3312.45,
        sl=0.0,
        tp=0.0,
        structure_bias=Bias.NEUTRAL,
        fu_candle_time=datetime.now(timezone.utc),
        confidence=["LuxAlgo Reversal"],
    )

    print("\nDispatching test LuxAlgo Reversal Up signal...")
    log_ids = await dispatcher.notify_signal(sig)
    print(f"Done! Log IDs: {log_ids}")

    # Also send a Reversal Down variant
    sig_dn = Signal(
        id="TEST-LUXALGO-DN-001",
        symbol="XAUUSD",
        timeframe="1m",
        direction=Direction.SELL,
        entry_price=3312.45,
        sl=0.0,
        tp=0.0,
        structure_bias=Bias.NEUTRAL,
        fu_candle_time=datetime.now(timezone.utc),
        confidence=["LuxAlgo Reversal"],
    )

    print("\nDispatching test LuxAlgo Reversal Down signal...")
    log_ids_dn = await dispatcher.notify_signal(sig_dn)
    print(f"Done! Log IDs: {log_ids_dn}")


if __name__ == "__main__":
    asyncio.run(main())
