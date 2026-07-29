import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from electricity_app.analytics import AnalyticsService
from electricity_app.config import Settings
from electricity_app.property_client import PropertyClient
from electricity_app.sync_service import SyncService


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        property_base_url="https://zf.zhongkeqizhi.cn:9000",
        property_username="authorized-user",
        property_password="authorized-password",
        database_path=tmp_path / "electricity.db",
        session_secret="s" * 32,
        openid_hmac_key="h" * 32,
        wechat_app_id="wx1234567890abcdef",
        wechat_app_secret="w" * 32,
        wechat_message_token="m" * 32,
        wechat_daily_template_id="daily-template-id",
        wechat_openid_encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        public_base_url="https://electricity.example.test",
    )


@pytest.fixture
def fixture_json() -> dict[str, object]:
    fixture_path = Path(__file__).parent / "fixtures" / "property_details.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))


@pytest.fixture
def client(settings: Settings):
    with httpx.Client(base_url=str(settings.property_base_url)) as http:
        yield PropertyClient(settings, http=http)


@pytest.fixture
def sync_service() -> Mock:
    return Mock(spec=SyncService)


@pytest.fixture
def analytics_service() -> Mock:
    return Mock(spec=AnalyticsService)
