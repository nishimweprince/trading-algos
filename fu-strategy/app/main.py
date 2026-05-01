"""FastAPI application entrypoint."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from app.api import notifications as notifications_api
from app.api import webhooks as webhooks_api
from app.config import get_settings
from app.data.feed import CapitalDataFeed
from app.engine.event_log import get_event_log
from app.engine.signal_generator import SignalGenerator
from app.execution.trade_executor import TradeExecutor
from app.jobs.poller import MarketPoller
from app.notifications.dispatcher import NotificationDispatcher
from app.notifications.log import NotificationLog
from app.notifications.sms_client import SMSClient
from app.notifications.whatsapp_client import WhatsAppClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info(f"Booting fu-strategy (notifications_enabled={settings.notifications_enabled}, "
                f"whatsapp_configured={settings.whatsapp_configured}, "
                f"sms_configured={settings.sms_configured}, "
                f"recipients={len(settings.notification_numbers)})")

    notification_log = NotificationLog(settings.notifications_log_path)
    await notification_log.init()

    whatsapp_client = WhatsAppClient(settings)
    sms_client = SMSClient(settings)
    dispatcher = NotificationDispatcher(settings, whatsapp_client, sms_client, notification_log)

    poller: MarketPoller | None = None
    capital_creds_present = bool(
        settings.capital_api_key and settings.capital_password and settings.capital_identifier
    )
    feed: CapitalDataFeed | None = None
    if settings.polling_enabled and capital_creds_present and settings.symbols:
        feed = CapitalDataFeed(
            settings.capital_api_key,
            settings.capital_password,
            settings.capital_identifier,
            settings.capital_environment,
        )

    executor: TradeExecutor | None = None
    if settings.capital_execution_enabled and capital_creds_present:
        if feed is None:
            feed = CapitalDataFeed(
                settings.capital_api_key,
                settings.capital_password,
                settings.capital_identifier,
                settings.capital_environment,
            )
        executor = TradeExecutor(
            feed.client, settings,
            get_event_log(settings.indicator_event_log_path),
            mapper=feed.mapper,
        )
        logger.info(
            f"TradeExecutor enabled: timeframe={settings.capital_execution_timeframe}, "
            f"size={settings.capital_execution_size}, env={settings.capital_environment}"
        )

    signal_generator = SignalGenerator.create(settings, executor=executor)

    app.state.settings = settings
    app.state.notification_log = notification_log
    app.state.whatsapp_client = whatsapp_client
    app.state.sms_client = sms_client
    app.state.dispatcher = dispatcher
    app.state.signal_generator = signal_generator

    app.include_router(webhooks_api.build_router(whatsapp_client, notification_log, dispatcher))
    app.include_router(notifications_api.build_router(notification_log, dispatcher))

    if settings.polling_enabled and feed is not None and settings.symbols:
        poller = MarketPoller(settings, signal_generator, dispatcher, feed)
        app.state.poller = poller
        await poller.start()
    elif settings.polling_enabled:
        logger.warning(
            "polling_enabled=true but Capital.com creds or SYMBOLS missing; poller not started"
        )

    try:
        yield
    finally:
        if poller is not None:
            await poller.stop()
        await whatsapp_client.aclose()
        await sms_client.aclose()
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
