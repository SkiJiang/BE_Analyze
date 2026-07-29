from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from electricity_app.db import Database
from electricity_app.domain import (
    AnalyticsPoint,
    DashboardSummary,
    DayDetail,
    ElectricityRecord,
    RangeAnalytics,
    TimeBucket,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
ZERO = Decimal("0")
STALE_AFTER = timedelta(minutes=90)
HALF_HOUR = timedelta(minutes=30)
NIGHT_BUCKET_COUNT = 11


class AnalyticsService:
    def __init__(
        self,
        database: Database,
        *,
        stale_after: timedelta = STALE_AFTER,
    ) -> None:
        self.database = database
        self.stale_after = stale_after

    def dashboard(self, now: datetime) -> DashboardSummary:
        local_now = _as_shanghai(now)
        today = local_now.date()
        records = self._records_for_local_dates(
            today - timedelta(days=30),
            today,
        )
        records_through_now = [
            record for record in records if _as_shanghai(record.occurred_at) <= local_now
        ]
        records_by_day = _records_by_local_day(records_through_now)

        today_records = records_by_day.get(today, [])
        yesterday_records = records_by_day.get(today - timedelta(days=1), [])
        today_energy, today_cost = _totals(today_records)
        yesterday_energy, _ = _totals(yesterday_records)
        day_change_percent = (
            None
            if yesterday_energy == ZERO
            else (
                (today_energy - yesterday_energy)
                / yesterday_energy
                * Decimal("100")
            ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        )

        seven_day_energy = _energy_for_date_range(
            records_by_day, today - timedelta(days=6), today
        )
        thirty_day_energy = _energy_for_date_range(
            records_by_day, today - timedelta(days=29), today
        )
        complete_days = _complete_day_totals(records_by_day, today)
        daily_average_energy = (
            sum((energy for energy, _ in complete_days), ZERO)
            / Decimal(len(complete_days))
            if len(complete_days) >= 3
            else None
        )

        today_buckets = _half_hour_buckets(today_records)
        peak_bucket = max(
            today_buckets,
            key=lambda bucket: (bucket.energy, -bucket.start.timestamp()),
            default=None,
        )
        balance = self.database.latest_balance()
        estimated_days_remaining = _estimated_days(balance, complete_days)
        recent_mean_energy, recent_mean_cost = _mean_totals(complete_days)
        recent_seven_day_change_percent = (
            None
            if recent_mean_energy is None or recent_mean_energy == ZERO
            else (
                (today_energy - recent_mean_energy)
                / recent_mean_energy
                * Decimal("100")
            ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        )
        recent_buckets = _recent_half_hour_buckets(
            local_now,
            records_through_now,
        )
        range_24h = _bucket_range("24h", recent_buckets)
        range_7d = _daily_range(
            "7d",
            today - timedelta(days=6),
            today,
            records_by_day,
        )
        range_30d = _daily_range(
            "30d",
            today - timedelta(days=29),
            today,
            records_by_day,
        )

        last_sync = self.database.last_successful_sync()
        last_successful_sync = last_sync.finished_at if last_sync is not None else None
        is_stale = (
            last_successful_sync is None
            or local_now - _as_shanghai(last_successful_sync) > self.stale_after
        )

        return DashboardSummary(
            balance=balance,
            today_energy=today_energy,
            today_cost=today_cost,
            yesterday_energy=yesterday_energy,
            day_change_percent=day_change_percent,
            seven_day_energy=seven_day_energy,
            thirty_day_energy=thirty_day_energy,
            daily_average_energy=daily_average_energy,
            peak_bucket=peak_bucket,
            range_24h=range_24h,
            range_7d=range_7d,
            range_30d=range_30d,
            recent_seven_day_mean_energy=recent_mean_energy,
            recent_seven_day_mean_cost=recent_mean_cost,
            recent_seven_day_change_percent=recent_seven_day_change_percent,
            typical_historical_peak_hour=_typical_historical_peak_hour(
                records_by_day,
                today,
            ),
            estimated_days_remaining=estimated_days_remaining,
            anomalies=_anomalies(records_by_day, today, local_now),
            last_successful_sync=last_successful_sync,
            is_stale=is_stale,
            hourly_profile=_hourly_profile(today, today_records),
            recent_buckets=recent_buckets,
        )

    def day_detail(self, day: date) -> DayDetail:
        records = self._records_for_local_dates(day, day)
        total_energy, total_cost = _totals(records)
        return DayDetail(
            day=day,
            total_energy=total_energy,
            total_cost=total_cost,
            buckets=_half_hour_buckets(records),
        )

    def rebuild_daily_summaries(
        self, start_date: date, end_date: date, now: datetime
    ) -> int:
        if start_date > end_date:
            raise ValueError("start_date must not be after end_date")
        local_now = _as_shanghai(now)
        history_start = start_date - timedelta(days=7)
        records = self._records_for_local_dates(history_start, end_date)
        records = [
            record
            for record in records
            if _as_shanghai(record.occurred_at) <= local_now
        ]
        records_by_day = _records_by_local_day(records)

        written = 0
        day = start_date
        while day <= end_date:
            day_records = records_by_day.get(day, [])
            total_energy, total_cost = _totals(day_records)
            buckets = _half_hour_buckets(day_records)
            peak = max(
                buckets,
                key=lambda bucket: (bucket.energy, -bucket.start.timestamp()),
                default=None,
            )
            baseline = _daily_baseline(records_by_day, day)
            anomaly_score = (
                (total_energy - baseline) / baseline
                if baseline is not None and baseline > ZERO
                else None
            )
            self.database.replace_daily_summary(
                day,
                total_energy=total_energy,
                total_cost=total_cost,
                record_count=len(day_records),
                peak_energy=peak.energy if peak is not None else None,
                peak_started_at=peak.start if peak is not None else None,
                baseline_energy=baseline,
                anomaly_score=anomaly_score,
            )
            written += 1
            day += timedelta(days=1)
        return written

    def _records_for_local_dates(
        self, start_date: date, end_date: date
    ) -> list[ElectricityRecord]:
        records = self.database.list_records(
            start_date - timedelta(days=1),
            end_date + timedelta(days=1),
        )
        return [
            record
            for record in records
            if start_date <= _as_shanghai(record.occurred_at).date() <= end_date
        ]


def _as_shanghai(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("analytics datetimes must be timezone-aware")
    return value.astimezone(SHANGHAI)


def _records_by_local_day(
    records: list[ElectricityRecord],
) -> dict[date, list[ElectricityRecord]]:
    by_day: dict[date, list[ElectricityRecord]] = defaultdict(list)
    for record in records:
        by_day[_as_shanghai(record.occurred_at).date()].append(record)
    return dict(by_day)


def _totals(records: list[ElectricityRecord]) -> tuple[Decimal, Decimal]:
    return (
        sum((record.energy for record in records), ZERO),
        sum((record.money for record in records), ZERO),
    )


def _half_hour_start(value: datetime) -> datetime:
    local_value = _as_shanghai(value)
    return local_value.replace(
        minute=30 if local_value.minute >= 30 else 0,
        second=0,
        microsecond=0,
    )


def _half_hour_buckets(records: list[ElectricityRecord]) -> tuple[TimeBucket, ...]:
    totals: dict[datetime, tuple[Decimal, Decimal]] = {}
    for record in records:
        start = _half_hour_start(record.occurred_at)
        energy, cost = totals.get(start, (ZERO, ZERO))
        totals[start] = (energy + record.energy, cost + record.money)
    return tuple(
        TimeBucket(start=start, energy=energy, cost=cost)
        for start, (energy, cost) in sorted(totals.items())
    )


def _energy_for_date_range(
    records_by_day: dict[date, list[ElectricityRecord]],
    start_date: date,
    end_date: date,
) -> Decimal:
    return sum(
        (
            record.energy
            for day, records in records_by_day.items()
            if start_date <= day <= end_date
            for record in records
        ),
        ZERO,
    )


def _complete_day_totals(
    records_by_day: dict[date, list[ElectricityRecord]], today: date
) -> list[tuple[Decimal, Decimal]]:
    totals: list[tuple[Decimal, Decimal]] = []
    for days_ago in range(1, 8):
        records = records_by_day.get(today - timedelta(days=days_ago))
        if records:
            totals.append(_totals(records))
    return totals


def _mean_totals(
    complete_days: list[tuple[Decimal, Decimal]],
) -> tuple[Decimal | None, Decimal | None]:
    if len(complete_days) < 3:
        return None, None
    count = Decimal(len(complete_days))
    return (
        sum((energy for energy, _ in complete_days), ZERO) / count,
        sum((cost for _, cost in complete_days), ZERO) / count,
    )


def _estimated_days(
    balance: Decimal | None,
    complete_days: list[tuple[Decimal, Decimal]],
) -> Decimal | None:
    if balance is None or balance <= ZERO or len(complete_days) < 3:
        return None
    mean_cost = (
        sum((cost for _, cost in complete_days), ZERO)
        / Decimal(len(complete_days))
    )
    return balance / mean_cost if mean_cost > ZERO else None


def _daily_baseline(
    records_by_day: dict[date, list[ElectricityRecord]], day: date
) -> Decimal | None:
    energies = [
        _totals(records)[0]
        for days_ago in range(1, 8)
        if (records := records_by_day.get(day - timedelta(days=days_ago)))
    ]
    if not energies:
        return None
    return sum(energies, ZERO) / Decimal(len(energies))


def _anomalies(
    records_by_day: dict[date, list[ElectricityRecord]],
    today: date,
    now: datetime,
) -> tuple[str, ...]:
    anomalies: list[str] = []
    if _is_high_vs_baseline(records_by_day, today, now):
        anomalies.append("high_vs_baseline")
    if _has_continuous_night_load(records_by_day, today, now):
        anomalies.append("continuous_night_load")
    return tuple(anomalies)


def _is_high_vs_baseline(
    records_by_day: dict[date, list[ElectricityRecord]],
    today: date,
    now: datetime,
) -> bool:
    prior_days = [today - timedelta(days=days_ago) for days_ago in range(1, 8)]

    elapsed = now.timetz().replace(tzinfo=None)
    today_energy = sum(
        (
            record.energy
            for record in records_by_day.get(today, [])
            if _as_shanghai(record.occurred_at).time() <= elapsed
        ),
        ZERO,
    )
    comparable_energies = [
        sum(
            (
                record.energy
                for record in records_by_day.get(day, [])
                if _as_shanghai(record.occurred_at).time() <= elapsed
            ),
            ZERO,
        )
        for day in prior_days
    ]
    baseline = sum(comparable_energies, ZERO) / Decimal("7")
    return (
        today_energy >= baseline * Decimal("1.5")
        and today_energy - baseline >= Decimal("1")
    )


def _has_continuous_night_load(
    records_by_day: dict[date, list[ElectricityRecord]],
    today: date,
    now: datetime,
) -> bool:
    night_finished_at = datetime.combine(today, time(5, 30), SHANGHAI)
    if now < night_finished_at:
        return False

    expected_today = [
        datetime.combine(today, time(), SHANGHAI) + bucket_number * HALF_HOUR
        for bucket_number in range(NIGHT_BUCKET_COUNT)
    ]
    today_buckets = {
        bucket.start: bucket.energy
        for bucket in _half_hour_buckets(records_by_day.get(today, []))
    }
    if any(today_buckets.get(start, ZERO) == ZERO for start in expected_today):
        return False

    previous_energies: list[Decimal] = []
    for days_ago in range(1, 8):
        day = today - timedelta(days=days_ago)
        starts = [
            datetime.combine(day, time(), SHANGHAI) + bucket_number * HALF_HOUR
            for bucket_number in range(NIGHT_BUCKET_COUNT)
        ]
        buckets = {
            bucket.start: bucket.energy
            for bucket in _half_hour_buckets(records_by_day.get(day, []))
        }
        previous_energies.extend(buckets.get(start, ZERO) for start in starts)

    current_mean = (
        sum((today_buckets[start] for start in expected_today), ZERO)
        / Decimal(NIGHT_BUCKET_COUNT)
    )
    previous_mean = sum(previous_energies, ZERO) / Decimal(len(previous_energies))
    return current_mean >= previous_mean * Decimal("1.5")


def _hourly_profile(
    day: date, records: list[ElectricityRecord]
) -> tuple[TimeBucket, ...]:
    totals: dict[datetime, tuple[Decimal, Decimal]] = {}
    for record in records:
        start = _as_shanghai(record.occurred_at).replace(
            minute=0, second=0, microsecond=0
        )
        energy, cost = totals.get(start, (ZERO, ZERO))
        totals[start] = (energy + record.energy, cost + record.money)
    day_start = datetime.combine(day, time(), SHANGHAI)
    return tuple(
        TimeBucket(
            start=start,
            energy=totals.get(start, (ZERO, ZERO))[0],
            cost=totals.get(start, (ZERO, ZERO))[1],
        )
        for start in (day_start + timedelta(hours=hour) for hour in range(24))
    )


def _bucket_range(
    key: str,
    buckets: tuple[TimeBucket, ...],
) -> RangeAnalytics:
    return RangeAnalytics(
        key=key,
        total_energy=sum((bucket.energy for bucket in buckets), ZERO),
        total_cost=sum((bucket.cost for bucket in buckets), ZERO),
        points=tuple(
            AnalyticsPoint(
                label=bucket.start.isoformat(),
                energy=bucket.energy,
                cost=bucket.cost,
            )
            for bucket in buckets
        ),
        highest_use_day=None,
        highest_use_day_energy=None,
    )


def _daily_range(
    key: str,
    start_date: date,
    end_date: date,
    records_by_day: dict[date, list[ElectricityRecord]],
) -> RangeAnalytics:
    points: list[AnalyticsPoint] = []
    populated: list[tuple[date, Decimal]] = []
    day = start_date
    while day <= end_date:
        records = records_by_day.get(day, [])
        energy, cost = _totals(records)
        points.append(
            AnalyticsPoint(
                label=day.isoformat(),
                energy=energy,
                cost=cost,
            )
        )
        if records:
            populated.append((day, energy))
        day += timedelta(days=1)

    highest = max(
        populated,
        key=lambda item: (item[1], -item[0].toordinal()),
        default=None,
    )
    return RangeAnalytics(
        key=key,
        total_energy=sum((point.energy for point in points), ZERO),
        total_cost=sum((point.cost for point in points), ZERO),
        points=tuple(points),
        highest_use_day=highest[0] if highest is not None else None,
        highest_use_day_energy=highest[1] if highest is not None else None,
    )


def _typical_historical_peak_hour(
    records_by_day: dict[date, list[ElectricityRecord]],
    today: date,
) -> int | None:
    historical_days = sorted(
        day
        for day, records in records_by_day.items()
        if day < today and records
    )
    if len(historical_days) < 3:
        return None

    energy_by_hour: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for day in historical_days:
        for record in records_by_day[day]:
            energy_by_hour[_as_shanghai(record.occurred_at).hour] += (
                record.energy
            )
    if not energy_by_hour:
        return None
    hour, energy = max(
        energy_by_hour.items(),
        key=lambda item: (item[1], -item[0]),
    )
    return hour if energy > ZERO else None


def _recent_half_hour_buckets(
    now: datetime, records: list[ElectricityRecord]
) -> tuple[TimeBucket, ...]:
    latest_start = _half_hour_start(now)
    starts = [latest_start - bucket_number * HALF_HOUR for bucket_number in range(47, -1, -1)]
    totals = {
        bucket.start: (bucket.energy, bucket.cost)
        for bucket in _half_hour_buckets(
            [
                record
                for record in records
                if starts[0] <= _as_shanghai(record.occurred_at) <= now
            ]
        )
    }
    return tuple(
        TimeBucket(
            start=start,
            energy=totals.get(start, (ZERO, ZERO))[0],
            cost=totals.get(start, (ZERO, ZERO))[1],
        )
        for start in starts
    )
