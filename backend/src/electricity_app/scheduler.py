"""Scheduled synchronization and analytics maintenance."""

from __future__ import annotations

import logging
from threading import Lock
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler

from electricity_app.analytics import AnalyticsService
from electricity_app.domain import SyncOutcome
from electricity_app.sync_service import SyncService
from electricity_app.reminders import DailyReminderService


LOGGER = logging.getLogger(__name__)


def create_scheduler(
    sync_service: SyncService,
    analytics_service: AnalyticsService,
    timezone: str,
    reminder_service: DailyReminderService | None = None,
) -> BackgroundScheduler:
    """Build the process-local scheduler without starting it."""
    scheduler = BackgroundScheduler(timezone=timezone)
    sync_lock = Lock()

    def synchronize(sync: Callable[[], SyncOutcome], job_id: str) -> None:
        if not sync_lock.acquire(blocking=False):
            LOGGER.info("Skipping overlapping synchronization", extra={"job": job_id})
            return
        try:
            outcome = sync()
            if outcome.status == "success":
                analytics_service.rebuild_daily_summaries(
                    outcome.start_date,
                    outcome.end_date,
                    outcome.finished_at,
                )
        finally:
            sync_lock.release()

    def sync_recent() -> None:
        synchronize(sync_service.sync_recent_now, "sync_recent")

    def reconcile_30_days() -> None:
        synchronize(
            sync_service.reconcile_30_days_now,
            "reconcile_30_days",
        )

    scheduler.add_job(
        sync_recent,
        "cron",
        minute="0,30",
        id="sync_recent",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
    )
    scheduler.add_job(
        reconcile_30_days,
        "cron",
        hour=2,
        minute=15,
        id="reconcile_30_days",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
    )
    if reminder_service is not None:
        scheduler.add_job(
            reminder_service.send_today,
            "cron",
            hour=23,
            minute=30,
            id="daily_reminder",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=900,
        )
    return scheduler
