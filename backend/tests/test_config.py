import pytest
from pydantic import ValidationError

from electricity_app.config import Settings


def valid_settings() -> dict[str, str]:
    return {
        "property_base_url": "https://zf.zhongkeqizhi.cn:9000",
        "property_username": "authorized-user",
        "property_password": "authorized-password",
        "database_path": "/tmp/electricity-test.db",
        "session_secret": "s" * 32,
        "openid_hmac_key": "h" * 32,
        "wechat_app_id": "wx1234567890abcdef",
        "wechat_app_secret": "w" * 32,
        "wechat_message_token": "m" * 32,
        "public_base_url": "https://electricity.example.test",
    }


def test_settings_accept_https_urls_and_strong_secrets():
    settings = Settings(**valid_settings())
    assert str(settings.property_base_url).startswith("https://")
    assert settings.poll_minutes == 30
    assert settings.timezone == "Asia/Shanghai"
    assert settings.property_room_name == "麒麟科创园-7号楼-805"
    assert settings.property_device_name == "7号楼/8F/805电表"
    assert settings.session_max_age_seconds == 1800


def test_settings_reject_http_property_endpoint():
    values = valid_settings()
    values["property_base_url"] = "http://property.example.test"
    with pytest.raises(ValidationError):
        Settings(**values)


def test_settings_reject_short_session_secret():
    values = valid_settings()
    values["session_secret"] = "short"
    with pytest.raises(ValidationError):
        Settings(**values)


def test_settings_rejects_non_shanghai_timezone():
    values = valid_settings()
    values["timezone"] = "UTC"

    with pytest.raises(ValidationError):
        Settings(**values)


def test_settings_rejects_a_long_lived_session():
    values = valid_settings()
    values["session_max_age_seconds"] = "7200"

    with pytest.raises(ValidationError):
        Settings(**values)


def test_settings_requires_a_strong_wechat_message_token():
    values = valid_settings()
    values.pop("wechat_message_token")

    with pytest.raises(ValidationError):
        Settings(**values)
