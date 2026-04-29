"""FastAPI application entrypoint."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from app.api import notifications as notifications_api
from app.api import webhooks as webhooks_api
from app.config import get_settings
from app.engine.signal_generator import SignalGenerator
from app.notifications.dispatcher import NotificationDispatcher
from app.notifications.log import NotificationLog
from app.notifications.whatsapp_client import WhatsAppClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info(f"Booting fu-strategy (notifications_enabled={settings.notifications_enabled}, "
                f"whatsapp_configured={settings.whatsapp_configured}, "
                f"recipients={len(settings.notification_numbers)})")

    notification_log = NotificationLog(settings.notifications_log_path)
    await notification_log.init()

    whatsapp_client = WhatsAppClient(settings)
    dispatcher = NotificationDispatcher(settings, whatsapp_client, notification_log)
    signal_generator = SignalGenerator.create(settings)

    app.state.settings = settings
    app.state.notification_log = notification_log
    app.state.whatsapp_client = whatsapp_client
    app.state.dispatcher = dispatcher
    app.state.signal_generator = signal_generator

    app.include_router(webhooks_api.build_router(whatsapp_client, notification_log, dispatcher))
    app.include_router(notifications_api.build_router(notification_log, dispatcher))

    try:
        yield
    finally:
        await whatsapp_client.aclose()
        logger.info("fu-strategy shut down")


app = FastAPI(
    title="FU Strategy API",
    description="FastAPI service for FU candle / multi-timeframe strategy.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "fu-strategy"}
