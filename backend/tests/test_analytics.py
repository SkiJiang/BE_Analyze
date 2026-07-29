from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from electricity_app.analytics import AnalyticsService
from electricity_app.db import Database
from electricity_app.domain import ElectricityRecord, SyncOutcome

TZ = ZoneInfo("Asia/Shanghai")


def make_record(
    key: str,
    occurred_at: datetime,
    *,
    energy: str,
    money: str,
    balance: str | None = None,
) -> ElectricityRecord:
    return ElectricityRecord(
        unique_key=key,
        upstream_id=key,
        room_name="Room 805",
        device_name="Meter 8F",
        occurred_at=occurred_at,
        energy=Decimal(energy),
        money=Decimal(money),
        rate=Decimal("0.55"),
        balance=Decimal(balance) if balance is not None else None,
    )


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), TZ)


def seed_database(
    database: Database,
    records: list[ElectricityRecord],
    *,
    balance: str | None = None,
    successful_sync_at: datetime | None = None,
) -> Database:
    if balance is not None:
        records = [
            *records,
            make_record(
                "balance-record",
                max(record.occurred_at for record in records) + timedelta(seconds=1),
                energy="0",
                money="0",
                balance=balance,
            ),
        ]

    if successful_sync_at is None:
        database.upsert_records(records)
        return database

    outcome = SyncOutcome(
        started_at=successful_sync_at - timedelta(minutes=1),
        finished_at=successful_sync_at,
        start_date=min(record.occurred_at.astimezone(TZ).date() for record in records),
        end_date=max(record.occurred_at.astimezone(TZ).date() for record in records),
        status="success",
        fetched=len(records),
        inserted=len(records),
        updated=0,
    )
    database.apply_sync(records, outcome, successful_sync_at)
    return database


@pytest.fixture
def analytics_db(tmp_path) -> Database:
    database = Database(tmp_path / "analytics.db")
    database.initialize()
    records = [
        make_record(
            f"history-{day.isoformat()}",
            at(day, 10),
            energy="2.0",
            money="1.10",
        )
        for day in (date(2026, 7, day) for day in range(22, 28))
    ]
    records.extend(
        [
            make_record("yesterday-a", at(date(2026, 7, 28), 10, 5), energy="1.0", money="0.55"),
            make_record("yesterday-b", at(date(2026, 7, 28), 11, 5), energy="2.0", money="1.10"),
            make_record("today-a", at(date(2026, 7, 29), 10, 5), energy="2.0", money="1.10"),
            make_record("today-b", at(date(2026, 7, 29), 10, 20), energy="1.2", money="0.66"),
            make_record("today-c", at(date(2026, 7, 29), 10, 30), energy="1.0", money="0.55"),
        ]
    )
    return seed_database(
        database,
        records,
        balance="18.20",
        successful_sync_at=datetime(2026, 7, 29, 12, 30, tzinfo=TZ),
    )


@pytest.fixture
def short_history_db(tmp_path) -> Database:
    database = Database(tmp_path / "short-history.db")
    database.initialize()
    records = [
        make_record("short-1", at(date(2026, 7, 27), 10), energy="1", money="1"),
        make_record("short-2", at(date(2026, 7, 28), 10), energy="1", money="1"),
    ]
    return seed_database(database, records, balance="10")


def test_dashboard_calculates_totals_and_day_comparison(analytics_db):
    summary = AnalyticsService(analytics_db).dashboard(
        datetime(2026, 7, 29, 12, 0, tzinfo=TZ)
    )

    assert summary.today_energy == Decimal("4.2")
    assert summary.today_cost == Decimal("2.31")
    assert summary.yesterday_energy == Decimal("3.0")
    assert summary.day_change_percent == Decimal("40.0")


def test_dashboard_calculates_rolling_totals_average_peak_and_balance(analytics_db):
    summary = AnalyticsService(analytics_db).dashboard(
        datetime(2026, 7, 29, 12, 0, tzinfo=TZ)
    )

    assert summary.balance == Decimal("18.20")
    assert summary.seven_day_energy == Decimal("17.2")
    assert summary.thirty_day_energy == Decimal("19.2")
    assert summary.daily_average_energy == Decimal("2.142857142857142857142857143")
    assert summary.peak_bucket is not None
    assert (
        summary.peak_bucket.start,
        summary.peak_bucket.energy,
        summary.peak_bucket.cost,
    ) == (
        at(date(2026, 7, 29), 10),
        Decimal("3.2"),
        Decimal("1.76"),
    )


def test_dashboard_exposes_energy_cost_and_points_for_all_ranges(
    analytics_db,
):
    summary = AnalyticsService(analytics_db).dashboard(
        datetime(2026, 7, 29, 12, 0, tzinfo=TZ)
    )

    assert (
        summary.range_24h.total_energy,
        summary.range_24h.total_cost,
        len(summary.range_24h.points),
    ) == (Decimal("4.2"), Decimal("2.31"), 48)
    assert (
        summary.range_7d.total_energy,
        summary.range_7d.total_cost,
        len(summary.range_7d.points),
    ) == (Decimal("17.2"), Decimal("9.46"), 7)
    assert (
        summary.range_30d.total_energy,
        summary.range_30d.total_cost,
        len(summary.range_30d.points),
    ) == (Decimal("19.2"), Decimal("10.56"), 30)
    assert summary.range_7d.highest_use_day == date(2026, 7, 29)
    assert summary.range_7d.highest_use_day_energy == Decimal("4.2")


def test_dashboard_compares_today_with_recent_seven_day_mean(analytics_db):
    summary = AnalyticsService(analytics_db).dashboard(
        datetime(2026, 7, 29, 12, 0, tzinfo=TZ)
    )

    assert summary.recent_seven_day_mean_energy == (
        Decimal("15") / Decimal("7")
    )
    assert summary.recent_seven_day_mean_cost == (
        Decimal("8.25") / Decimal("7")
    )
    assert summary.recent_seven_day_change_percent == Decimal("96.0")


def test_range_highest_day_uses_earliest_day_for_equal_energy(tmp_path):
    database = Database(tmp_path / "highest-day.db")
    database.initialize()
    seed_database(
        database,
        [
            make_record(
                "earliest-high",
                at(date(2026, 7, 27), 10),
                energy="5",
                money="2",
            ),
            make_record(
                "later-high",
                at(date(2026, 7, 28), 10),
                energy="5",
                money="3",
            ),
        ],
    )

    summary = AnalyticsService(database).dashboard(
        at(date(2026, 7, 29), 12)
    )

    assert summary.range_7d.highest_use_day == date(2026, 7, 27)
    assert summary.range_7d.highest_use_day_energy == Decimal("5")


def test_typical_historical_peak_is_distinct_from_todays_peak(tmp_path):
    database = Database(tmp_path / "typical-peak.db")
    database.initialize()
    records = [
        make_record(
            f"history-{day}",
            at(date(2026, 7, day), 18),
            energy="2",
            money="1",
        )
        for day in (26, 27, 28)
    ]
    records.append(
        make_record(
            "today-peak",
            at(date(2026, 7, 29), 10),
            energy="10",
            money="5",
        )
    )
    seed_database(database, records)

    summary = AnalyticsService(database).dashboard(
        at(date(2026, 7, 29), 12)
    )

    assert summary.peak_bucket is not None
    assert summary.peak_bucket.start.hour == 10
    assert summary.typical_historical_peak_hour == 18


def test_insufficient_history_returns_null_mean_and_typical_peak(
    short_history_db,
):
    summary = AnalyticsService(short_history_db).dashboard(
        datetime(2026, 7, 29, 12, 0, tzinfo=TZ)
    )

    assert summary.recent_seven_day_mean_energy is None
    assert summary.recent_seven_day_mean_cost is None
    assert summary.recent_seven_day_change_percent is None
    assert summary.typical_historical_peak_hour is None


def test_balance_days_is_none_when_history_is_insufficient(short_history_db):
    summary = AnalyticsService(short_history_db).dashboard(
        datetime(2026, 7, 29, 12, 0, tzinfo=TZ)
    )

    assert summary.estimated_days_remaining is None


def test_dashboard_is_stale_after_90_minutes(analytics_db):
    summary = AnalyticsService(analytics_db).dashboard(
        datetime(2026, 7, 29, 14, 1, tzinfo=TZ)
    )

    assert summary.is_stale is True


def test_rebuild_daily_summaries_persists_one_row_per_day(analytics_db):
    service = AnalyticsService(analytics_db)
    raw_record_count = analytics_db.count_records()

    written = service.rebuild_daily_summaries(
        date(2026, 7, 28),
        date(2026, 7, 29),
        datetime(2026, 7, 29, 14, 1, tzinfo=TZ),
    )
    summaries = analytics_db.list_daily_summaries(date(2026, 7, 28), date(2026, 7, 29))

    assert written == 2
    assert analytics_db.count_records() == raw_record_count
    assert len(summaries) == 2
    assert [
        (summary["day"], summary["total_energy"], summary["total_cost"])
        for summary in summaries
    ] == [
        (date(2026, 7, 28), Decimal("3.0"), Decimal("1.65")),
        (date(2026, 7, 29), Decimal("4.2"), Decimal("2.31")),
    ]


def test_day_detail_sums_records_in_the_same_bucket_and_keeps_boundary_separate(analytics_db):
    detail = AnalyticsService(analytics_db).day_detail(date(2026, 7, 29))

    assert detail.total_energy == Decimal("4.2")
    assert detail.total_cost == Decimal("2.31")
    assert [
        (bucket.start, bucket.energy, bucket.cost) for bucket in detail.buckets
    ] == [
        (at(date(2026, 7, 29), 10), Decimal("3.2"), Decimal("1.76")),
        (at(date(2026, 7, 29), 10, 30), Decimal("1.0"), Decimal("0.55")),
    ]


def test_day_detail_uses_shanghai_boundaries_for_non_shanghai_record_offsets(tmp_path):
    database = Database(tmp_path / "timezone.db")
    database.initialize()
    seed_database(
        database,
        [
            make_record(
                "local-july-29",
                datetime(2026, 7, 28, 16, 15, tzinfo=UTC),
                energy="1.25",
                money="0.70",
            ),
            make_record(
                "local-july-30",
                datetime(2026, 7, 29, 16, 0, tzinfo=UTC),
                energy="9",
                money="9",
            ),
        ],
    )

    detail = AnalyticsService(database).day_detail(date(2026, 7, 29))

    assert detail.total_energy == Decimal("1.25")
    assert detail.buckets[0].start == at(date(2026, 7, 29), 0)


def test_day_comparison_is_none_when_yesterday_energy_is_zero(tmp_path):
    database = Database(tmp_path / "zero-yesterday.db")
    database.initialize()
    seed_database(
        database,
        [make_record("today", at(date(2026, 7, 29), 9), energy="2", money="1")],
    )

    summary = AnalyticsService(database).dashboard(at(date(2026, 7, 29), 12))

    assert summary.day_change_percent is None


def test_estimated_days_uses_up_to_seven_latest_complete_days_and_excludes_today(tmp_path):
    database = Database(tmp_path / "estimate.db")
    database.initialize()
    records = [
        make_record("complete-1", at(date(2026, 7, 26), 10), energy="1", money="2"),
        make_record("complete-2", at(date(2026, 7, 27), 10), energy="1", money="4"),
        make_record("complete-3", at(date(2026, 7, 28), 10), energy="1", money="6"),
        make_record("partial-today", at(date(2026, 7, 29), 10), energy="1", money="100"),
    ]
    seed_database(
        database,
        records,
        balance="20",
        successful_sync_at=at(date(2026, 7, 29), 11),
    )

    summary = AnalyticsService(database).dashboard(at(date(2026, 7, 29), 12))

    assert summary.estimated_days_remaining == Decimal("5")


def test_estimated_days_is_none_when_latest_balance_is_not_positive(tmp_path):
    database = Database(tmp_path / "zero-balance.db")
    database.initialize()
    records = [
        make_record(
            f"complete-{day}",
            at(date(2026, 7, day), 10),
            energy="1",
            money="2",
        )
        for day in range(26, 29)
    ]
    seed_database(
        database,
        records,
        balance="0",
        successful_sync_at=at(date(2026, 7, 29), 11),
    )

    summary = AnalyticsService(database).dashboard(at(date(2026, 7, 29), 12))

    assert summary.estimated_days_remaining is None


def test_estimated_days_is_none_when_mean_daily_cost_is_not_positive(tmp_path):
    database = Database(tmp_path / "zero-cost.db")
    database.initialize()
    records = [
        make_record(
            f"complete-{day}",
            at(date(2026, 7, day), 10),
            energy="1",
            money="0",
        )
        for day in range(26, 29)
    ]
    seed_database(
        database,
        records,
        balance="20",
        successful_sync_at=at(date(2026, 7, 29), 11),
    )

    summary = AnalyticsService(database).dashboard(at(date(2026, 7, 29), 12))

    assert summary.estimated_days_remaining is None


def test_high_vs_baseline_uses_same_elapsed_time_and_inclusive_thresholds(tmp_path):
    database = Database(tmp_path / "high-baseline.db")
    database.initialize()
    records = [
        make_record(
            f"baseline-{day}",
            at(date(2026, 7, day), 10),
            energy="2",
            money="1",
        )
        for day in range(22, 29)
    ]
    records.extend(
        [
            make_record("today-comparable", at(date(2026, 7, 29), 10), energy="3", money="1"),
            make_record("today-after-now", at(date(2026, 7, 29), 13), energy="20", money="1"),
        ]
    )
    seed_database(database, records)

    summary = AnalyticsService(database).dashboard(at(date(2026, 7, 29), 12))

    assert "high_vs_baseline" in summary.anomalies
    assert summary.today_energy == Decimal("3")


def test_high_vs_baseline_zero_fills_empty_prior_days(tmp_path):
    database = Database(tmp_path / "empty-prior-days.db")
    database.initialize()
    records = [
        make_record(
            "one-prior-day",
            at(date(2026, 7, 28), 10),
            energy="1",
            money="1",
        ),
        make_record(
            "today-comparable",
            at(date(2026, 7, 29), 10),
            energy="1.5",
            money="1",
        ),
    ]
    seed_database(database, records)

    summary = AnalyticsService(database).dashboard(at(date(2026, 7, 29), 12))

    assert "high_vs_baseline" in summary.anomalies


def test_high_vs_baseline_applies_inclusive_one_kwh_threshold_to_zero_baseline(tmp_path):
    database = Database(tmp_path / "zero-comparable-baseline.db")
    database.initialize()
    seed_database(
        database,
        [
            make_record(
                "today-comparable",
                at(date(2026, 7, 29), 10),
                energy="1",
                money="1",
            )
        ],
    )

    summary = AnalyticsService(database).dashboard(at(date(2026, 7, 29), 12))

    assert "high_vs_baseline" in summary.anomalies


def test_continuous_night_load_requires_every_bucket_through_0500(tmp_path):
    database = Database(tmp_path / "night-load.db")
    database.initialize()
    records: list[ElectricityRecord] = []
    for previous_day in range(22, 29):
        day = date(2026, 7, previous_day)
        for bucket_number in range(11):
            minute = bucket_number * 30
            records.append(
                make_record(
                    f"baseline-{previous_day}-{bucket_number}",
                    at(day, minute // 60, minute % 60),
                    energy="0.1",
                    money="0.05",
                )
            )
    for bucket_number in range(11):
        minute = bucket_number * 30
        records.append(
            make_record(
                f"today-{bucket_number}",
                at(date(2026, 7, 29), minute // 60, minute % 60),
                energy="0.2",
                money="0.1",
            )
        )
    seed_database(database, records)

    complete = AnalyticsService(database).dashboard(at(date(2026, 7, 29), 6))
    database.upsert_records(
        [
            make_record(
                "today-10",
                at(date(2026, 7, 29), 5),
                energy="0",
                money="0",
            )
        ]
    )
    missing_one = AnalyticsService(database).dashboard(at(date(2026, 7, 29), 6))

    assert "continuous_night_load" in complete.anomalies
    assert "continuous_night_load" not in missing_one.anomalies


def test_continuous_night_load_zero_fills_absent_prior_nights(tmp_path):
    database = Database(tmp_path / "night-load-no-history.db")
    database.initialize()
    records = []
    for bucket_number in range(11):
        minute = bucket_number * 30
        records.append(
            make_record(
                f"today-{bucket_number}",
                at(date(2026, 7, 29), minute // 60, minute % 60),
                energy="0.1",
                money="0.05",
            )
        )
    seed_database(database, records)

    summary = AnalyticsService(database).dashboard(at(date(2026, 7, 29), 6))

    assert "continuous_night_load" in summary.anomalies


def test_continuous_night_load_treats_a_nonzero_adjustment_bucket_as_present(tmp_path):
    database = Database(tmp_path / "night-load-adjustment.db")
    database.initialize()
    records = []
    for bucket_number in range(11):
        minute = bucket_number * 30
        records.append(
            make_record(
                f"today-{bucket_number}",
                at(date(2026, 7, 29), minute // 60, minute % 60),
                energy="-0.1" if bucket_number == 0 else "0.2",
                money="0",
            )
        )
    seed_database(database, records)

    summary = AnalyticsService(database).dashboard(at(date(2026, 7, 29), 6))

    assert "continuous_night_load" in summary.anomalies


def test_continuous_night_load_includes_exact_fifty_percent_threshold_with_sparse_history(
    tmp_path,
):
    database = Database(tmp_path / "night-load-threshold.db")
    database.initialize()
    records = []
    for bucket_number in range(11):
        minute = bucket_number * 30
        records.extend(
            [
                make_record(
                    f"prior-{bucket_number}",
                    at(date(2026, 7, 28), minute // 60, minute % 60),
                    energy="0.7",
                    money="0.35",
                ),
                make_record(
                    f"today-{bucket_number}",
                    at(date(2026, 7, 29), minute // 60, minute % 60),
                    energy="0.15",
                    money="0.08",
                ),
            ]
        )
    seed_database(database, records)

    summary = AnalyticsService(database).dashboard(at(date(2026, 7, 29), 6))

    assert "continuous_night_load" in summary.anomalies


def test_staleness_boundary_is_strict_and_missing_sync_is_stale(analytics_db, short_history_db):
    service = AnalyticsService(analytics_db)

    assert service.dashboard(datetime(2026, 7, 29, 14, 0, tzinfo=TZ)).is_stale is False
    assert (
        service.dashboard(datetime(2026, 7, 29, 14, 0, 0, 1, tzinfo=TZ)).is_stale
        is True
    )
    assert AnalyticsService(short_history_db).dashboard(at(date(2026, 7, 29), 12)).is_stale is True


def test_rebuild_rejects_an_inverted_date_range(analytics_db):
    with pytest.raises(ValueError, match="start_date"):
        AnalyticsService(analytics_db).rebuild_daily_summaries(
            date(2026, 7, 30),
            date(2026, 7, 29),
            at(date(2026, 7, 30), 12),
        )
