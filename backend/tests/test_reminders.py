from datetime import datetime
from decimal import Decimal
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

from electricity_app.domain import DashboardSummary
from electricity_app.reminders import DailyReminderService


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_daily_reminder_sends_template_once_per_recipient(settings, tmp_path):
    from electricity_app.db import Database

    database = Database(tmp_path / "reminder.db")
    database.initialize()
    digest = "recipient-digest"
    request_id = database.upsert_pending_openid(digest)
    assert database.set_openid_enabled(request_id, True)
    encrypted_openid = Fernet(
        settings.wechat_openid_encryption_key.get_secret_value().encode()
    ).encrypt(b"openid-value").decode()
    assert database.save_authorized_openid(digest, encrypted_openid)

    now = datetime(2026, 7, 29, 20, tzinfo=SHANGHAI)
    summary = Mock(spec=DashboardSummary)
    summary.is_stale = False
    summary.today_energy = Decimal("4.5")
    summary.today_cost = Decimal("2.7")
    summary.balance = Decimal("10")
    summary.seven_day_energy = Decimal("20")
    summary.last_successful_sync = now
    analytics = Mock()
    analytics.dashboard.return_value = summary
    client = Mock()
    service = DailyReminderService(settings, database, analytics, client)

    assert service.send_today(now) == 1
    assert service.send_today(now) == 0
    client.send_template.assert_called_once()
    openid, values = client.send_template.call_args.args
    assert openid == "openid-value"
    assert values["request_url"] == "https://electricity.example.test/wechat/entry"


def test_daily_reminder_skips_stale_data(settings, tmp_path):
    from electricity_app.db import Database

    summary = Mock(spec=DashboardSummary)
    summary.is_stale = True
    analytics = Mock()
    analytics.dashboard.return_value = summary
    service = DailyReminderService(
        settings, Database(tmp_path / "reminder.db"), analytics, Mock()
    )

    assert service.send_today(datetime(2026, 7, 29, 20, tzinfo=SHANGHAI)) == 0
