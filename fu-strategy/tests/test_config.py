from app.config import Settings


def test_fu_sma_length_defaults_to_50_and_can_be_overridden():
    assert Settings(_env_file=None).fu_sma_length == 50
    assert Settings(_env_file=None, fu_sma_length=21).fu_sma_length == 21


def test_notification_channels_parse_and_normalize_csv():
    settings = Settings(
        _env_file=None,
        notification_channels='SMS, WHATSAPP, unknown',
    )

    assert settings.notification_channels == ['sms', 'whatsapp']
    assert settings.sms_notifications_allowed is True
    assert settings.whatsapp_notifications_allowed is True


def test_notification_channels_can_allow_sms_only():
    settings = Settings(
        _env_file=None,
        notification_channels='sms',
    )

    assert settings.notification_channels == ['sms']
    assert settings.sms_notifications_allowed is True
    assert settings.whatsapp_notifications_allowed is False
