from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ElectricityRecord:
    unique_key: str
    upstream_id: str | None
    room_name: str
    device_name: str
    occurred_at: datetime
    energy: Decimal
    money: Decimal
    rate: Decimal | None
    balance: Decimal | None


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    started_at: datetime
    finished_at: datetime
    start_date: date
    end_date: date
    status: str
    fetched: int
    inserted: int
    updated: int
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class TimeBucket:
    start: datetime
    energy: Decimal
    cost: Decimal


@dataclass(frozen=True, slots=True)
class AnalyticsPoint:
    label: str
    energy: Decimal
    cost: Decimal


@dataclass(frozen=True, slots=True)
class RangeAnalytics:
    key: str
    total_energy: Decimal
    total_cost: Decimal
    points: tuple[AnalyticsPoint, ...]
    highest_use_day: date | None
    highest_use_day_energy: Decimal | None


@dataclass(frozen=True, slots=True)
class DashboardSummary:
    balance: Decimal | None
    today_energy: Decimal
    today_cost: Decimal
    yesterday_energy: Decimal
    day_change_percent: Decimal | None
    seven_day_energy: Decimal
    thirty_day_energy: Decimal
    daily_average_energy: Decimal | None
    peak_bucket: TimeBucket | None
    range_24h: RangeAnalytics
    range_7d: RangeAnalytics
    range_30d: RangeAnalytics
    recent_seven_day_mean_energy: Decimal | None
    recent_seven_day_mean_cost: Decimal | None
    recent_seven_day_change_percent: Decimal | None
    typical_historical_peak_hour: int | None
    estimated_days_remaining: Decimal | None
    anomalies: tuple[str, ...]
    last_successful_sync: datetime | None
    is_stale: bool
    hourly_profile: tuple[TimeBucket, ...]
    recent_buckets: tuple[TimeBucket, ...]


@dataclass(frozen=True, slots=True)
class DayDetail:
    day: date
    total_energy: Decimal
    total_cost: Decimal
    buckets: tuple[TimeBucket, ...]
