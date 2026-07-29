import argparse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from electricity_app.analytics import AnalyticsService
from electricity_app.config import get_settings
from electricity_app.db import Database
from electricity_app.property_client import PropertyClient
from electricity_app.sync_service import SyncService
from electricity_app.wechat_template import WeChatTemplateClient
from electricity_app.reminders import DailyReminderService
import httpx


_PROBE_REQUIRED_FIELDS = (
    "balance",
    "deviceName",
    "energy",
    "id",
    "money",
    "rate",
    "roomName",
    "time",
)
def main() -> None:
    """Run the administrative commands."""
    parser = argparse.ArgumentParser(prog="electricity-admin")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init-db")
    subcommands.add_parser("list-pending")
    subcommands.add_parser("probe-property-schema")
    sync_date = subcommands.add_parser("sync-date")
    sync_date.add_argument("day", type=date.fromisoformat)
    summarize_date = subcommands.add_parser("summarize-date")
    summarize_date.add_argument("day", type=date.fromisoformat)
    enable_wechat = subcommands.add_parser("enable-wechat")
    enable_wechat.add_argument("request_id", type=int)
    disable_wechat = subcommands.add_parser("disable-wechat")
    disable_wechat.add_argument("request_id", type=int)
    subcommands.add_parser("reset-property-auth")
    subcommands.add_parser("send-daily-reminder")
    args = parser.parse_args()

    settings = get_settings()
    database = Database(settings.database_path)
    database.initialize()

    if args.command == "init-db":
        return
    if args.command == "list-pending":
        for request_id, created_at in database.list_pending_openids():
            print(request_id, created_at.isoformat())
        return
    if args.command == "enable-wechat":
        if not database.set_openid_enabled(args.request_id, True):
            raise SystemExit(1)
        return
    if args.command == "disable-wechat":
        if not database.set_openid_enabled(args.request_id, False):
            raise SystemExit(1)
        return
    if args.command == "reset-property-auth":
        if not database.clear_auth_gate():
            raise SystemExit(1)
        return
    if args.command == "send-daily-reminder":
        analytics = AnalyticsService(database)
        with httpx.Client(verify=True, timeout=httpx.Timeout(10, read=30)) as http:
            sent = DailyReminderService(
                settings,
                database,
                analytics,
                WeChatTemplateClient(settings, http=http),
            ).send_today()
        print(f"sent={sent}")
        return
    if args.command == "probe-property-schema":
        property_client = PropertyClient(settings)
        current_day = datetime.now(ZoneInfo(settings.timezone)).date()
        records = property_client.fetch_day(current_day)
        field_names = property_client.last_field_names
        print(f"field_names={','.join(field_names)}")
        print(f"record_count={len(records)}")
        for field in _PROBE_REQUIRED_FIELDS:
            print(f"required_field.{field}={field in field_names}")
        return
    if args.command == "sync-date":
        outcome = SyncService(PropertyClient(settings), database).sync_dates(
            args.day, args.day
        )
        print(f"date={args.day.isoformat()}")
        print(f"status={outcome.status}")
        print(f"fetched={outcome.fetched}")
        print(f"inserted={outcome.inserted}")
        print(f"updated={outcome.updated}")
        if outcome.error_code is not None:
            print(f"error_code={outcome.error_code}")
        if outcome.status != "success":
            raise SystemExit(1)
        return
    if args.command == "summarize-date":
        detail = AnalyticsService(database).day_detail(args.day)
        records = database.list_records(
            args.day - timedelta(days=1),
            args.day + timedelta(days=1),
        )
        record_count = sum(
            record.occurred_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
            == args.day
            for record in records
        )
        latest_balance = database.latest_balance_for_day(args.day)
        print(f"date={args.day.isoformat()}")
        print(f"record_count={record_count}")
        print(f"total_energy={detail.total_energy}")
        print(f"total_cost={detail.total_cost}")
        print(
            "latest_balance="
            + (str(latest_balance) if latest_balance is not None else "unavailable")
        )
        for bucket in detail.buckets:
            bucket_label = bucket.start.strftime("%H:%M")
            print(f"bucket.{bucket_label}.energy={bucket.energy}")
            print(f"bucket.{bucket_label}.cost={bucket.cost}")
