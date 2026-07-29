from datetime import date, datetime
from decimal import Decimal
import sqlite3
from zoneinfo import ZoneInfo

import pytest

import electricity_app.sync_service as sync_service
from electricity_app.db import Database
from electricity_app.domain import ElectricityRecord
from electricity_app.property_client import (
    PropertyAuthenticationError,
    PropertyProtocolError,
    PropertyUnavailableError,
)
from electricity_app.sync_service import SyncService

TZ = ZoneInfo("Asia/Shanghai")


class FakeClient:
    def __init__(self) -> None:
        self.requested_days: list[date] = []

    def fetch_day(self, day: date) -> list[ElectricityRecord]:
        self.requested_days.append(day)
        return [record(day)]


class FailingClient:
    def __init__(self, error: Exception = PropertyProtocolError("bad response")) -> None:
        self.error = error
        self.requested_days: list[date] = []

    def fetch_day(self, day: date) -> list[ElectricityRecord]:
        self.requested_days.append(day)
        raise self.error


class FirstDayThenFailsClient:
    def __init__(self) -> None:
        self.requested_days: list[date] = []

    def fetch_day(self, day: date) -> list[ElectricityRecord]:
        self.requested_days.append(day)
        if len(self.requested_days) == 1:
            return [record(day)]
        raise RuntimeError("unexpected property client failure")


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def failing_client() -> FailingClient:
    return FailingClient()


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "electricity.db")
    database.initialize()
    return database


@pytest.fixture
def populated_db(db: Database) -> Database:
    db.upsert_records([record(date(2026, 7, 27))])
    return db


def record(day: date) -> ElectricityRecord:
    return ElectricityRecord(
        unique_key=f"record-{day.isoformat()}",
        upstream_id=day.isoformat(),
        room_name="Room 805",
        device_name="Meter 8F",
        occurred_at=datetime(day.year, day.month, day.day, 10, 7, tzinfo=TZ),
        energy=Decimal("0.1"),
        money=Decimal("0.06"),
        rate=Decimal("0.55"),
        balance=Decimal("182.66"),
    )


def test_recent_sync_fetches_today_and_yesterday(fake_client: FakeClient, db: Database):
    now = datetime(2026, 7, 29, 10, 30, tzinfo=TZ)

    outcome = SyncService(fake_client, db).sync_recent(now)

    assert fake_client.requested_days == [date(2026, 7, 28), date(2026, 7, 29)]
    assert outcome.status == "success"
    assert outcome.fetched == 2
    assert db.count_records() == 2


def test_failed_sync_keeps_existing_records(failing_client: FailingClient, populated_db: Database):
    before = populated_db.count_records()

    outcome = SyncService(failing_client, populated_db).sync_recent(
        datetime(2026, 7, 29, 10, 30, tzinfo=TZ)
    )

    assert outcome.status == "failed"
    assert populated_db.count_records() == before
    assert populated_db.last_successful_sync() is None


def test_authentication_failure_requires_new_authorization(db: Database):
    client = FailingClient(PropertyAuthenticationError("token rejected"))
    service = SyncService(client, db)
    outcome = service.sync_dates(date(2026, 7, 29), date(2026, 7, 29))

    assert outcome.status == "auth_required"
    assert outcome.error_code == "authentication"
    assert db.count_records() == 0

    gated_outcome = service.sync_dates(
        date(2026, 7, 29), date(2026, 7, 29)
    )
    assert gated_outcome.status == "auth_required"
    assert gated_outcome.error_code == "auth_gate"
    assert client.requested_days == [date(2026, 7, 29)]

    db.clear_auth_gate()
    service.sync_dates(date(2026, 7, 29), date(2026, 7, 29))
    assert client.requested_days == [
        date(2026, 7, 29),
        date(2026, 7, 29),
    ]


def test_generic_failure_after_a_fetched_day_preserves_records_and_records_one_failed_run(
    populated_db: Database,
):
    client = FirstDayThenFailsClient()
    before = populated_db.count_records()

    outcome = SyncService(client, populated_db).sync_recent(
        datetime(2026, 7, 29, 10, 30, tzinfo=TZ)
    )

    with sqlite3.connect(populated_db.database_path) as connection:
        statuses = [row[0] for row in connection.execute("SELECT status FROM sync_runs")]
    assert outcome.status == "failed"
    assert populated_db.count_records() == before
    assert statuses == ["failed"]


def test_property_unavailable_failure_has_failed_status(db: Database):
    outcome = SyncService(
        FailingClient(
            PropertyUnavailableError(
                "service unavailable",
                code="upstream_5xx",
            )
        ),
        db,
    ).sync_dates(date(2026, 7, 29), date(2026, 7, 29))

    assert outcome.status == "failed"
    assert outcome.error_code == "upstream_5xx"


def test_protocol_diagnostics_preserve_safe_category_only(db: Database):
    outcome = SyncService(
        FailingClient(
            PropertyProtocolError(
                "sensitive upstream details",
                code="invalid_json",
            )
        ),
        db,
    ).sync_dates(date(2026, 7, 29), date(2026, 7, 29))

    assert outcome.status == "failed"
    assert outcome.error_code == "invalid_json"


def test_reconciliation_fetches_exactly_thirty_calendar_days(fake_client: FakeClient, db: Database):
    SyncService(fake_client, db).reconcile_30_days(datetime(2026, 7, 29, 10, 30, tzinfo=TZ))

    assert fake_client.requested_days == [date(2026, 6, 30), *[date(2026, 7, day) for day in range(1, 30)]]


def test_sync_dates_rejects_an_inverted_date_range(fake_client: FakeClient, db: Database):
    with pytest.raises(ValueError, match="start_date"):
        SyncService(fake_client, db).sync_dates(date(2026, 7, 30), date(2026, 7, 29))


def test_now_methods_use_shanghai_calendar_day(fake_client: FakeClient, db: Database, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == TZ
            return datetime(2026, 7, 29, 10, 30, tzinfo=TZ)

    monkeypatch.setattr(sync_service, "datetime", FixedDatetime)
    service = SyncService(fake_client, db)

    service.sync_recent_now()
    service.reconcile_30_days_now()

    assert fake_client.requested_days[:2] == [date(2026, 7, 28), date(2026, 7, 29)]
    assert fake_client.requested_days[2:] == [
        date(2026, 6, 30),
        *[date(2026, 7, day) for day in range(1, 30)],
    ]
