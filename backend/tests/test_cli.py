from datetime import date, datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from electricity_app.db import Database
from electricity_app.domain import ElectricityRecord, SyncOutcome


TZ = ZoneInfo("Asia/Shanghai")


def _record(
    unique_key: str,
    occurred_at: datetime,
    *,
    energy: str,
    money: str,
    balance: str,
) -> ElectricityRecord:
    return ElectricityRecord(
        unique_key=unique_key,
        upstream_id=f"sensitive-upstream-{unique_key}",
        room_name="sensitive-room",
        device_name="sensitive-device",
        occurred_at=occurred_at,
        energy=Decimal(energy),
        money=Decimal(money),
        rate=Decimal("0.55"),
        balance=Decimal(balance),
    )


def test_sync_date_persists_one_day_and_prints_only_safe_counts(
    tmp_path, monkeypatch, capsys
):
    from electricity_app import cli

    database_path = tmp_path / "test.db"
    settings = SimpleNamespace(database_path=database_path)
    expected_day = date(2026, 7, 29)
    property_record = _record(
        "detail-1",
        datetime(2026, 7, 29, 10, 7, 7, tzinfo=TZ),
        energy="0.1",
        money="0.06",
        balance="182.66",
    )

    class FakePropertyClient:
        def __init__(self, received_settings):
            assert received_settings is settings

        def fetch_day(self, day):
            assert day == expected_day
            return [property_record]

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "PropertyClient", FakePropertyClient)
    monkeypatch.setattr(
        "sys.argv", ["electricity-admin", "sync-date", expected_day.isoformat()]
    )

    cli.main()

    database = Database(database_path)
    stored = database.list_records(expected_day, expected_day)
    assert stored == [property_record]
    assert capsys.readouterr().out == (
        "date=2026-07-29\n"
        "status=success\n"
        "fetched=1\n"
        "inserted=1\n"
        "updated=0\n"
    )


def test_sync_date_exits_nonzero_without_printing_upstream_error_body(
    tmp_path, monkeypatch, capsys
):
    from electricity_app import cli
    from electricity_app.property_client import PropertyAuthenticationError

    database_path = tmp_path / "test.db"
    settings = SimpleNamespace(database_path=database_path)

    class FailingPropertyClient:
        def __init__(self, received_settings):
            assert received_settings is settings

        def fetch_day(self, day):
            raise PropertyAuthenticationError(
                "raw response with property-password and property-token"
            )

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "PropertyClient", FailingPropertyClient)
    monkeypatch.setattr(
        "sys.argv", ["electricity-admin", "sync-date", "2026-07-29"]
    )

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 1
    assert capsys.readouterr().out == (
        "date=2026-07-29\n"
        "status=auth_required\n"
        "fetched=0\n"
        "inserted=0\n"
        "updated=0\n"
        "error_code=authentication\n"
    )


def test_summarize_date_prints_comparable_totals_balance_and_half_hour_buckets(
    tmp_path, monkeypatch, capsys
):
    from electricity_app import cli

    database_path = tmp_path / "test.db"
    database = Database(database_path)
    database.initialize()
    expected_day = date(2026, 7, 29)
    records = [
        _record(
            "detail-1",
            datetime(2026, 7, 29, 10, 7, 7, tzinfo=TZ),
            energy="0.1",
            money="0.06",
            balance="182.66",
        ),
        _record(
            "detail-2",
            datetime(2026, 7, 29, 10, 37, 7, tzinfo=TZ),
            energy="0.2",
            money="0.11",
            balance="182.55",
        ),
        _record(
            "detail-3",
            datetime(2026, 7, 29, 11, 7, 7, tzinfo=TZ),
            energy="0.3",
            money="0.17",
            balance="182.38",
        ),
    ]
    database.upsert_records(records)
    # A balance snapshot is written by the synchronization transaction.
    observed_at = datetime(2026, 7, 29, 11, 8, tzinfo=TZ)
    database.apply_sync(
        records,
        SyncOutcome(
            started_at=observed_at,
            finished_at=observed_at,
            start_date=expected_day,
            end_date=expected_day,
            status="success",
            fetched=3,
            inserted=0,
            updated=0,
        ),
        observed_at,
    )

    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(database_path=database_path),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["electricity-admin", "summarize-date", expected_day.isoformat()],
    )

    cli.main()

    output = capsys.readouterr().out
    assert output == (
        "date=2026-07-29\n"
        "record_count=3\n"
        "total_energy=0.6\n"
        "total_cost=0.34\n"
        "latest_balance=182.38\n"
        "bucket.10:00.energy=0.1\n"
        "bucket.10:00.cost=0.06\n"
        "bucket.10:30.energy=0.2\n"
        "bucket.10:30.cost=0.11\n"
        "bucket.11:00.energy=0.3\n"
        "bucket.11:00.cost=0.17\n"
    )
    assert "sensitive-room" not in output
    assert "sensitive-device" not in output
    assert "sensitive-upstream" not in output


def test_summarize_date_counts_records_by_shanghai_calendar_day(
    tmp_path, monkeypatch, capsys
):
    from electricity_app import cli

    database_path = tmp_path / "test.db"
    database = Database(database_path)
    database.initialize()
    database.upsert_records(
        [
            _record(
                "utc-detail",
                datetime(2026, 7, 28, 16, 7, tzinfo=timezone.utc),
                energy="0.1",
                money="0.06",
                balance="182.66",
            )
        ]
    )
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(database_path=database_path),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["electricity-admin", "summarize-date", "2026-07-29"],
    )

    cli.main()

    output = capsys.readouterr().out
    assert "record_count=1\n" in output
    assert "total_energy=0.1\n" in output


def test_summarize_date_uses_selected_day_balance_when_a_newer_day_exists(
    tmp_path, monkeypatch, capsys
):
    from electricity_app import cli

    database_path = tmp_path / "test.db"
    database = Database(database_path)
    database.initialize()
    selected_day = date(2026, 7, 29)
    selected_record = _record(
        "selected-day",
        datetime(2026, 7, 29, 23, 37, tzinfo=TZ),
        energy="0.1",
        money="0.06",
        balance="182.38",
    )
    newer_record = _record(
        "newer-day",
        datetime(2026, 7, 30, 0, 7, tzinfo=TZ),
        energy="0.2",
        money="0.11",
        balance="999.99",
    )
    for record_value, observed_at in (
        (selected_record, datetime(2026, 7, 29, 23, 38, tzinfo=TZ)),
        (newer_record, datetime(2026, 7, 30, 0, 8, tzinfo=TZ)),
    ):
        database.apply_sync(
            [record_value],
            SyncOutcome(
                started_at=observed_at,
                finished_at=observed_at,
                start_date=record_value.occurred_at.date(),
                end_date=record_value.occurred_at.date(),
                status="success",
                fetched=1,
                inserted=1,
                updated=0,
            ),
            observed_at,
        )
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(database_path=database_path),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["electricity-admin", "summarize-date", selected_day.isoformat()],
    )

    cli.main()

    assert "latest_balance=182.38\n" in capsys.readouterr().out


def test_summarize_date_reports_unavailable_for_empty_day_with_newer_balance(
    tmp_path, monkeypatch, capsys
):
    from electricity_app import cli

    database_path = tmp_path / "test.db"
    database = Database(database_path)
    database.initialize()
    other_day = date(2026, 7, 30)
    other_record = _record(
        "other-day",
        datetime(2026, 7, 30, 10, 7, tzinfo=TZ),
        energy="0.2",
        money="0.11",
        balance="999.99",
    )
    observed_at = datetime(2026, 7, 30, 10, 8, tzinfo=TZ)
    database.apply_sync(
        [other_record],
        SyncOutcome(
            started_at=observed_at,
            finished_at=observed_at,
            start_date=other_day,
            end_date=other_day,
            status="success",
            fetched=1,
            inserted=1,
            updated=0,
        ),
        observed_at,
    )
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(database_path=database_path),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["electricity-admin", "summarize-date", "2026-07-29"],
    )

    cli.main()

    output = capsys.readouterr().out
    assert "record_count=0\n" in output
    assert "latest_balance=unavailable\n" in output
    assert "999.99" not in output


def test_disable_wechat_revokes_enabled_request(tmp_path, monkeypatch):
    from electricity_app import cli

    database_path = tmp_path / "test.db"
    database = Database(database_path)
    database.initialize()
    request_id = database.upsert_pending_openid("digest-value")
    assert database.set_openid_enabled(request_id, True)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(database_path=database_path),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["electricity-admin", "disable-wechat", str(request_id)],
    )

    cli.main()

    assert database.is_openid_allowed("digest-value") is False


@pytest.mark.parametrize(
    "arguments",
    [
        ["disable-wechat", "1"],
        ["disable-wechat", "999"],
        ["disable-wechat", "invalid"],
        ["disable-wechat"],
    ],
)
def test_disable_wechat_invalid_already_disabled_or_missing_id_exits_nonzero(
    tmp_path,
    monkeypatch,
    arguments,
):
    from electricity_app import cli

    database_path = tmp_path / "test.db"
    database = Database(database_path)
    database.initialize()
    database.upsert_pending_openid("disabled-digest")
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(database_path=database_path),
    )
    monkeypatch.setattr("sys.argv", ["electricity-admin", *arguments])

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code != 0


def test_reset_property_auth_clears_gate_for_running_scheduler(
    tmp_path,
    monkeypatch,
):
    from electricity_app import cli

    database_path = tmp_path / "test.db"
    database = Database(database_path)
    database.initialize()
    finished_at = datetime(2026, 7, 29, 10, 30, tzinfo=TZ)
    database.apply_sync(
        [],
        SyncOutcome(
            started_at=finished_at,
            finished_at=finished_at,
            start_date=date(2026, 7, 29),
            end_date=date(2026, 7, 29),
            status="auth_required",
            fetched=0,
            inserted=0,
            updated=0,
            error_code="authentication",
        ),
        finished_at,
    )
    assert database.auth_gate_active() is True
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(database_path=database_path),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["electricity-admin", "reset-property-auth"],
    )

    cli.main()

    assert database.auth_gate_active() is False


def test_backup_cli_creates_consistent_restricted_copy_and_prunes_retention(
    tmp_path,
    monkeypatch,
):
    from electricity_app import cli

    database_path = tmp_path / "test.db"
    database = Database(database_path)
    database.initialize()
    database.upsert_records(
        [
            _record(
                "backup-detail",
                datetime(2026, 7, 29, 10, 7, tzinfo=TZ),
                energy="1",
                money="0.55",
                balance="100",
            )
        ]
    )
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    expired = backup_directory / "electricity-20260101T000000Z.db"
    expired.write_bytes(b"expired")
    unrelated = backup_directory / "manual-copy.db"
    unrelated.write_bytes(b"keep")
    old_timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(expired, (old_timestamp, old_timestamp))
    os.utime(unrelated, (old_timestamp, old_timestamp))

    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(database_path=database_path),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "electricity-admin",
            "backup-db",
            str(backup_directory),
            "--retention-days",
            "30",
        ],
    )
    monkeypatch.setattr(
        cli.Database,
        "initialize",
        lambda self: (_ for _ in ()).throw(
            AssertionError("backup must not initialize the source database")
        ),
    )

    cli.main()

    backups = list(backup_directory.glob("electricity-*.db"))
    assert expired not in backups
    assert len(backups) == 1
    assert unrelated.exists()
    assert not list(backup_directory.glob("*.tmp"))
    if os.name == "posix":
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM electricity_records"
        ).fetchone()[0] == 1
