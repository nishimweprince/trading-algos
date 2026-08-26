"""The notification-service client, bound to this service's source name.

The implementation is ta-notify's; this module exists only to fix `source` so
call sites keep constructing `Notifier(settings, http)`.
"""

from __future__ import annotations

import httpx
from ta_notify import Notifier as _Notifier

from .config import Settings

SOURCE = "session-hedging"

__all__ = ["SOURCE", "Notifier"]


class Notifier(_Notifier):
    """SOURCE stays "session-hedging" deliberately.

    It is what the notification-service delivery log and the operator's filters
    already key on; renaming it with the directory would silently split the
    history of a running strategy.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        super().__init__(settings, client, source=SOURCE)
