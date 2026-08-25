"""Atomic synchronization of property electricity records."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from electricity_app.db import Database
from electricity_app.domain import ElectricityRecord, SyncOutcome
from electricity_app.property_client import (
    PropertyAuthenticationError,
    PropertyClient,
    PropertyProtocolError,
    PropertyUnavailableError,
)


_SHANGHAI = ZoneInfo("Asia/Shanghai")


class SyncService:
    def __init__(self, client: PropertyClient, db: Database) -> None:
        self._client = client
        self._db = db

    def sync_dates(self, start_date: date, end_date: date) -> SyncOutcome:
        if start_date > end_date:
            raise ValueError("start_date must not be after end_date")

        started_at = datetime.now(_SHANGHAI)
        if self._db.auth_gate_active():
            return self._record_failure(
                started_at,
                start_date,
                end_date,
                "auth_required",
                PropertyAuthenticationError(
                    "Property authentication attempts are gated",
                    code="auth_gate",
                ),
            )
        records: list[ElectricityRecord] = []
        current_date = start_date
        try:
            while current_date <= end_date:
                records.extend(self._client.fetch_day(current_date))
                current_date += timedelta(days=1)
        except PropertyAuthenticationError as exc:
            return self._record_failure(
                started_at, start_date, end_date, "auth_required", exc
            )
        except (PropertyProtocolError, PropertyUnavailableError) as exc:
            return self._record_failure(started_at, start_date, end_date, "failed", exc)
        except Exception as exc:
            return self._record_failure(started_at, start_date, end_date, "failed", exc)

        finished_at = datetime.now(_SHANGHAI)
        account_balance = None
        fetch_balance = getattr(self._client, "fetch_balance", None)
        if fetch_balance is not None:
            try:
                account_balance = fetch_balance()
            except PropertyAuthenticationError as exc:
                return self._record_failure(
                    started_at, start_date, end_date, "auth_required", exc
                )
            except (PropertyProtocolError, PropertyUnavailableError):
                # Keep valid energy records when the separate balance endpoint is unavailable.
                account_balance = None
        pending_outcome = SyncOutcome(
            started_at=started_at,
            finished_at=finished_at,
            start_date=start_date,
            end_date=end_date,
            status="success",
            fetched=len(records),
            inserted=0,
            updated=0,
        )
        inserted, updated = self._db.apply_sync(
            records,
            pending_outcome,
            finished_at,
            account_balance=account_balance,
        )
        return SyncOutcome(
            started_at=started_at,
            finished_at=finished_at,
            start_date=start_date,
            end_date=end_date,
            status="success",
            fetched=len(records),
            inserted=inserted,
            updated=updated,
        )

    def sync_recent(self, now: datetime) -> SyncOutcome:
        today = now.astimezone(_SHANGHAI).date()
        return self.sync_dates(today - timedelta(days=1), today)

    def reconcile_30_days(self, now: datetime) -> SyncOutcome:
        today = now.astimezone(_SHANGHAI).date()
        return self.sync_dates(today - timedelta(days=29), today)

    def sync_recent_now(self) -> SyncOutcome:
        return self.sync_recent(datetime.now(_SHANGHAI))

    def reconcile_30_days_now(self) -> SyncOutcome:
        return self.reconcile_30_days(datetime.now(_SHANGHAI))

    def _record_failure(
        self,
        started_at: datetime,
        start_date: date,
        end_date: date,
        status: str,
        error: Exception,
    ) -> SyncOutcome:
        outcome = SyncOutcome(
            started_at=started_at,
            finished_at=datetime.now(_SHANGHAI),
            start_date=start_date,
            end_date=end_date,
            status=status,
            fetched=0,
            inserted=0,
            updated=0,
            error_code=getattr(error, "code", "internal"),
        )
        self._db.apply_sync([], outcome, outcome.finished_at)
        return outcome
