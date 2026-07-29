from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest

from electricity_app.db import Database
from electricity_app.domain import ElectricityRecord, SyncOutcome

TZ = ZoneInfo("Asia/Shanghai")


class TrackingConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.was_closed = False

    def close(self) -> None:
        self.was_closed = True
        super().close()


class TrackingDatabase(Database):
    def __init__(self, database_path):
        super().__init__(database_path)
        self.opened_connections: list[TrackingConnection] = []

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            factory=TrackingConnection,
        )
        self.opened_connections.append(connection)
        return connection


def record(balance: str = "182.66") -> ElectricityRecord:
    return ElectricityRecord(
        unique_key="upstream-1",
        upstream_id="1",
        room_name="楹掗簾绉戝垱鍥?7鍙锋ゼ-805",
        device_name="7鍙锋ゼ/8F/805鐢佃〃",
        occurred_at=datetime(2026, 7, 29, 10, 7, 7, tzinfo=TZ),
        energy=Decimal("0.1"),
        money=Decimal("0.06"),
        rate=Decimal("0.55"),
        balance=Decimal(balance),
    )


def test_database_closes_connections_after_success_and_failure(tmp_path):
    db = TrackingDatabase(tmp_path / "tracked.db")
    db.initialize()

    db.count_records()
    successful_connection = db.opened_connections[-1]

    now = datetime(2026, 7, 29, 12, 0, tzinfo=TZ)
    db.create_oauth_nonce(
        "duplicate-nonce",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.create_oauth_nonce(
            "duplicate-nonce",
            created_at=now,
            expires_at=now + timedelta(minutes=5),
        )
    failing_connection = db.opened_connections[-1]

    assert successful_connection.was_closed is True
    assert failing_connection.was_closed is True


def test_historical_backfill_cannot_replace_current_balance(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    current_record = replace(
        record("100.00"),
        unique_key="current-detail",
        occurred_at=datetime(2026, 7, 29, 12, 0, tzinfo=TZ),
    )
    historical_record = replace(
        record("500.00"),
        unique_key="historical-detail",
        occurred_at=datetime(2026, 7, 28, 23, 0, tzinfo=TZ),
    )
    current_sync = SyncOutcome(
        started_at=datetime(2026, 7, 29, 12, 1, tzinfo=TZ),
        finished_at=datetime(2026, 7, 29, 12, 1, tzinfo=TZ),
        start_date=date(2026, 7, 29),
        end_date=date(2026, 7, 29),
        status="success",
        fetched=1,
        inserted=1,
        updated=0,
    )
    backfill_sync = replace(
        current_sync,
        started_at=datetime(2026, 7, 29, 12, 5, tzinfo=TZ),
        finished_at=datetime(2026, 7, 29, 12, 5, tzinfo=TZ),
        start_date=date(2026, 7, 28),
        end_date=date(2026, 7, 28),
    )

    db.apply_sync([current_record], current_sync, current_sync.finished_at)
    db.apply_sync(
        [historical_record],
        backfill_sync,
        backfill_sync.finished_at,
    )

    assert db.latest_balance() == Decimal("100.00")
    assert db.latest_balance_for_day(date(2026, 7, 28)) == Decimal("500.00")


def test_upsert_is_idempotent(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()

    assert db.upsert_records([record()]) == (1, 0)
    assert db.upsert_records([record("182.60")]) == (0, 1)

    rows = db.list_records(date(2026, 7, 29), date(2026, 7, 29))
    assert len(rows) == 1
    assert rows[0].balance == Decimal("182.60")


def test_allowlist_starts_disabled_and_can_be_enabled(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()

    request_id = db.upsert_pending_openid("digest-value")

    assert db.is_openid_allowed("digest-value") is False
    db.set_openid_enabled(request_id, True)
    assert db.is_openid_allowed("digest-value") is True


def test_enabled_allowlist_stores_encrypted_openid_for_reminders(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    request_id = db.upsert_pending_openid("digest-value")
    assert db.set_openid_enabled(request_id, True)

    assert db.save_authorized_openid("digest-value", "ciphertext") is True
    assert db.list_reminder_recipients() == [("digest-value", "ciphertext")]


def test_enabling_wechat_request_requires_a_disabled_row(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    request_id = db.upsert_pending_openid("digest-value")

    assert db.set_openid_enabled(request_id, True) is True
    assert db.set_openid_enabled(request_id, True) is False


def test_oauth_nonce_can_be_consumed_only_once(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    now = datetime(2026, 7, 29, 12, 0, tzinfo=TZ)
    db.create_oauth_nonce(
        "nonce-digest",
        created_at=now - timedelta(seconds=300),
        expires_at=now,
    )

    assert db.consume_oauth_nonce("nonce-digest", consumed_at=now) is True
    assert db.consume_oauth_nonce("nonce-digest", consumed_at=now) is False


def test_oauth_nonce_is_valid_through_300_seconds_and_expires_after(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    expires_at = datetime(2026, 7, 29, 12, 5, tzinfo=TZ)
    created_at = expires_at - timedelta(seconds=300)
    db.create_oauth_nonce(
        "boundary-digest",
        created_at=created_at,
        expires_at=expires_at,
    )
    db.create_oauth_nonce(
        "expired-digest",
        created_at=created_at,
        expires_at=expires_at,
    )

    assert (
        db.consume_oauth_nonce("boundary-digest", consumed_at=expires_at) is True
    )
    assert (
        db.consume_oauth_nonce(
            "expired-digest",
            consumed_at=datetime(2026, 7, 29, 12, 5, 0, 1, tzinfo=TZ),
        )
        is False
    )


def test_oauth_nonce_expiry_compares_instants_across_utc_offsets(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.create_oauth_nonce(
        "nonce-digest",
        created_at=datetime(2026, 7, 29, 12, 0, tzinfo=TZ),
        expires_at=datetime(2026, 7, 29, 12, 5, tzinfo=TZ),
    )

    consumed = db.consume_oauth_nonce(
        "nonce-digest",
        consumed_at=datetime(
            2026, 7, 29, 4, 5, 0, 1, tzinfo=timezone.utc
        ),
    )

    assert consumed is False


def test_creating_oauth_nonce_prunes_only_previously_expired_abandoned_rows(
    tmp_path,
):
    db = Database(tmp_path / "test.db")
    db.initialize()
    old_expiry = datetime(2026, 7, 29, 12, 5, tzinfo=TZ)
    prune_time = datetime(2026, 7, 29, 12, 5, 0, 1, tzinfo=TZ)
    db.create_oauth_nonce(
        "expired-digest",
        created_at=datetime(2026, 7, 29, 12, 0, tzinfo=TZ),
        expires_at=old_expiry,
    )
    db.create_oauth_nonce(
        "unexpired-digest",
        created_at=datetime(2026, 7, 29, 12, 1, tzinfo=TZ),
        expires_at=datetime(2026, 7, 29, 12, 6, tzinfo=TZ),
    )

    db.create_oauth_nonce(
        "new-digest",
        created_at=prune_time,
        expires_at=datetime(2026, 7, 29, 12, 10, 0, 1, tzinfo=TZ),
    )

    with sqlite3.connect(db.database_path) as connection:
        stored_digests = {
            row[0]
            for row in connection.execute(
                "SELECT nonce_digest FROM oauth_nonces"
            )
        }
    assert stored_digests == {"unexpired-digest", "new-digest"}


def test_oauth_nonce_consumption_is_atomic_across_connections(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    now = datetime(2026, 7, 29, 12, 0, tzinfo=TZ)
    db.create_oauth_nonce(
        "nonce-digest",
        created_at=now - timedelta(seconds=300),
        expires_at=now,
    )
    barrier = Barrier(2)

    def consume() -> bool:
        barrier.wait()
        return db.consume_oauth_nonce("nonce-digest", consumed_at=now)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: consume(), range(2)))

    assert sorted(results) == [False, True]


def test_apply_sync_persists_records_snapshot_and_successful_run(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    started_at = datetime(2026, 7, 29, 10, 0, tzinfo=TZ)
    finished_at = datetime(2026, 7, 29, 10, 1, tzinfo=TZ)
    outcome = SyncOutcome(
        started_at=started_at,
        finished_at=finished_at,
        start_date=date(2026, 7, 28),
        end_date=date(2026, 7, 29),
        status="success",
        fetched=1,
        inserted=1,
        updated=0,
    )

    inserted, updated = db.apply_sync([record()], outcome, finished_at)

    assert (inserted, updated) == (1, 0)
    assert db.count_records() == 1
    assert db.latest_balance() == Decimal("182.66")
    assert db.last_successful_sync() == outcome


def test_latest_balance_for_day_uses_latest_record_on_selected_shanghai_day(
    tmp_path,
):
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.upsert_records(
        [
            replace(
                record("183.00"),
                unique_key="selected-early",
                occurred_at=datetime(2026, 7, 29, 0, 7, tzinfo=TZ),
            ),
            replace(
                record("182.38"),
                unique_key="selected-latest",
                occurred_at=datetime(2026, 7, 29, 23, 59, tzinfo=TZ),
            ),
            replace(
                record("999.99"),
                unique_key="newer-other-day",
                occurred_at=datetime(2026, 7, 29, 16, 0, tzinfo=timezone.utc),
            ),
        ]
    )

    assert db.latest_balance_for_day(date(2026, 7, 29)) == Decimal("182.38")


def test_latest_balance_for_day_returns_none_when_selected_day_has_no_records(
    tmp_path,
):
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.upsert_records(
        [
            replace(
                record("999.99"),
                unique_key="other-day",
                occurred_at=datetime(2026, 7, 30, 10, 7, tzinfo=TZ),
            )
        ]
    )

    assert db.latest_balance_for_day(date(2026, 7, 29)) is None


def test_latest_sync_status_returns_the_most_recent_run(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    first_finished_at = datetime(2026, 7, 29, 10, 1, tzinfo=TZ)
    second_finished_at = datetime(2026, 7, 29, 10, 2, tzinfo=TZ)

    assert db.latest_sync_status() is None
    for status, finished_at in (
        ("auth_required", first_finished_at),
        ("failed", second_finished_at),
    ):
        db.apply_sync(
            [],
            SyncOutcome(
                started_at=finished_at,
                finished_at=finished_at,
                start_date=date(2026, 7, 29),
                end_date=date(2026, 7, 29),
                status=status,
                fetched=0,
                inserted=0,
                updated=0,
            ),
            finished_at,
        )

    assert db.latest_sync_status() == "failed"


def test_apply_sync_persists_actual_upsert_counts_not_stale_outcome_counts(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    outcome = SyncOutcome(
        started_at=datetime(2026, 7, 29, 10, 0, tzinfo=TZ),
        finished_at=datetime(2026, 7, 29, 10, 1, tzinfo=TZ),
        start_date=date(2026, 7, 28),
        end_date=date(2026, 7, 29),
        status="success",
        fetched=1,
        inserted=99,
        updated=88,
    )

    db.apply_sync([record()], outcome, outcome.finished_at)

    persisted_outcome = db.last_successful_sync()
    assert persisted_outcome is not None
    assert (persisted_outcome.inserted, persisted_outcome.updated) == (1, 0)


def test_daily_summary_replaces_only_its_day(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    july_28 = date(2026, 7, 28)
    july_29 = date(2026, 7, 29)

    db.replace_daily_summary(
        july_28,
        total_energy=Decimal("1.0"),
        total_cost=Decimal("0.55"),
        record_count=10,
        peak_energy=Decimal("0.2"),
        peak_started_at=datetime(2026, 7, 28, 18, tzinfo=TZ),
        baseline_energy=Decimal("0.1"),
        anomaly_score=Decimal("0.5"),
    )
    db.replace_daily_summary(
        july_29,
        total_energy=Decimal("2.0"),
        total_cost=Decimal("1.10"),
        record_count=20,
        peak_energy=None,
        peak_started_at=None,
        baseline_energy=None,
        anomaly_score=None,
    )
    db.replace_daily_summary(
        july_29,
        total_energy=Decimal("3.0"),
        total_cost=Decimal("1.65"),
        record_count=30,
        peak_energy=None,
        peak_started_at=None,
        baseline_energy=None,
        anomaly_score=None,
    )

    summaries = db.list_daily_summaries(july_28, july_29)

    assert [(summary["day"], summary["total_energy"], summary["record_count"]) for summary in summaries] == [
        (july_28, Decimal("1.0"), 10),
        (july_29, Decimal("3.0"), 30),
    ]


def test_list_pending_cli_hides_openid_hmac(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from electricity_app import cli

    database_path = tmp_path / "test.db"
    db = Database(database_path)
    db.initialize()
    request_id = db.upsert_pending_openid("secret-hmac-digest")
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(database_path=database_path))
    monkeypatch.setattr("sys.argv", ["electricity-admin", "list-pending"])

    cli.main()

    output = capsys.readouterr().out
    assert str(request_id) in output
    assert "secret-hmac-digest" not in output
