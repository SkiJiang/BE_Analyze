"""Daily WeChat template-message orchestration."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken

from electricity_app.analytics import AnalyticsService
from electricity_app.config import Settings
from electricity_app.db import Database
from electricity_app.wechat_template import WeChatTemplateClient


LOGGER = logging.getLogger(__name__)


class DailyReminderService:
    """Send one current-day summary to each enabled, authorized recipient."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        analytics: AnalyticsService,
        client: WeChatTemplateClient,
    ) -> None:
        self._settings = settings
        self._database = database
        self._analytics = analytics
        self._client = client
        self._cipher = Fernet(
            settings.wechat_openid_encryption_key.get_secret_value().encode()
        )
        self._timezone = ZoneInfo(settings.timezone)

    def send_today(self, now: datetime | None = None) -> int:
        """Send current data once per recipient, returning successful deliveries."""
        local_now = (now or datetime.now(self._timezone)).astimezone(self._timezone)
        summary = self._analytics.dashboard(local_now)
        if summary.is_stale:
            LOGGER.warning("Skipping daily reminder because electricity data is stale")
            return 0

        values = {
            "room": self._settings.property_device_name,
            "date": local_now.date().isoformat(),
            # Units and the currency sign are part of the WeChat template text.
            "today_energy": _format_decimal(summary.today_energy),
            "today_cost": _format_decimal(summary.today_cost),
            "balance": _format_optional_decimal(summary.balance),
            "week_energy": _format_decimal(summary.seven_day_energy),
            "updated_at": _format_datetime(summary.last_successful_sync),
            "request_url": str(self._settings.public_base_url).rstrip("/")
            + "/wechat/entry",
        }
        sent = 0
        for openid_hmac, ciphertext in self._database.list_reminder_recipients():
            if self._database.reminder_was_sent(local_now.date(), openid_hmac):
                continue
            try:
                openid = self._cipher.decrypt(ciphertext.encode()).decode()
            except (InvalidToken, UnicodeDecodeError):
                LOGGER.warning("Skipping unusable authorized reminder recipient")
                continue
            self._client.send_template(openid, values)
            self._database.record_reminder_sent(
                local_now.date(), openid_hmac, local_now
            )
            sent += 1
        return sent


def _format_decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _format_optional_decimal(value: Decimal | None) -> str:
    return _format_decimal(value) if value is not None else "unavailable"


def _format_datetime(value: datetime | None) -> str:
    return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M") if value else "unavailable"
