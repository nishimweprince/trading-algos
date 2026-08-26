"""The one notification-service client. See README.md."""

from .client import Notifier, SupportsNotification
from .settings import ALLOWED_CHANNELS, NotificationSettings

__all__ = ["ALLOWED_CHANNELS", "NotificationSettings", "Notifier", "SupportsNotification"]
