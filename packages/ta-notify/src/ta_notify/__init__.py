"""The one notification-service client. See README.md."""

from .client import NotificationResult, Notifier, SupportsNotification, SyncNotifier
from .settings import ALLOWED_CHANNELS, NotificationSettings

__all__ = [
    "ALLOWED_CHANNELS",
    "NotificationResult",
    "NotificationSettings",
    "Notifier",
    "SupportsNotification",
    "SyncNotifier",
]
