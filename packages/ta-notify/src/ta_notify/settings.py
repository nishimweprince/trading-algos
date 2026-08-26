"""The NOTIFICATION_* environment fields, as a mixin for a service's settings."""

from __future__ import annotations

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_CHANNELS = frozenset({"TELEGRAM", "EMAIL", "SMS", "WHATSAPP"})


class NotificationSettings(BaseSettings):
    # Mirrors BaseServiceSettings, which this is normally mixed into. Repeated
    # rather than inherited so the mixin is also usable on its own; without
    # populate_by_name the aliased fields cannot be set by field name.
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    notifications_enabled: bool = Field(default=False, validation_alias="NOTIFICATIONS_ENABLED")
    notification_service_url: str = Field(
        default="http://127.0.0.1:3010", min_length=1, validation_alias="NOTIFICATION_SERVICE_URL"
    )
    notification_api_key: SecretStr | None = Field(
        default=None, validation_alias="NOTIFICATION_API_KEY"
    )
    notification_channels_csv: str = Field(
        default="TELEGRAM", validation_alias="NOTIFICATION_CHANNELS"
    )
    notification_timeout_seconds: float = Field(
        default=30.0, gt=0, validation_alias="NOTIFICATION_TIMEOUT_SECONDS"
    )

    @property
    def notification_channels(self) -> frozenset[str]:
        return frozenset(
            token.strip().upper()
            for token in self.notification_channels_csv.split(",")
            if token.strip()
        )

    @model_validator(mode="after")
    def validate_notification_channels(self) -> NotificationSettings:
        unknown = self.notification_channels - ALLOWED_CHANNELS
        if unknown:
            raise ValueError(
                f"unknown NOTIFICATION_CHANNELS: {', '.join(sorted(unknown))}; "
                f"allowed: {', '.join(sorted(ALLOWED_CHANNELS))}"
            )
        if self.notifications_enabled and not self.notification_channels:
            raise ValueError("NOTIFICATION_CHANNELS is required when NOTIFICATIONS_ENABLED=true")
        return self
