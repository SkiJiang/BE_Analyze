"""FastAPI application composition for the electricity dashboard."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import logging
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI
import httpx
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from electricity_app.analytics import AnalyticsService
from electricity_app.config import Settings, get_settings
from electricity_app.db import Database
from electricity_app.property_client import PropertyClient
from electricity_app.scheduler import create_scheduler
from electricity_app.sync_service import SyncService
from electricity_app.web import TEMPLATES, create_router


PACKAGE_DIRECTORY = Path(__file__).resolve().parent
_SENSITIVE_KEY = re.compile(
    r"password|token|cookie|authorization|openid|code",
    re.IGNORECASE,
)


class SensitiveDataFilter(logging.Filter):
    """Remove secrets from structured logging values."""

    _electricity_app_sensitive_data = True

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.msg)
        record.args = _redact(record.args)
        for key, value in tuple(record.__dict__.items()):
            if _SENSITIVE_KEY.search(key):
                record.__dict__[key] = "[redacted]"
            elif key not in {"msg", "args"}:
                record.__dict__[key] = _redact(value)
        return True


def create_app(settings: Settings | None = None) -> FastAPI:
    """Compose the application and its process-local resources."""
    resolved_settings = settings or get_settings()
    install_sensitive_log_filter()

    database = Database(resolved_settings.database_path)
    database.initialize()
    database.clear_auth_gate()

    property_http = httpx.Client(
        base_url=str(resolved_settings.property_base_url),
        verify=True,
        timeout=httpx.Timeout(10, read=30),
        follow_redirects=False,
    )
    property_client = PropertyClient(resolved_settings, http=property_http)
    wechat_http = httpx.Client(
        verify=True,
        timeout=httpx.Timeout(10, read=30),
        follow_redirects=False,
    )
    sync_service = SyncService(property_client, database)
    analytics_service = AnalyticsService(
        database,
        stale_after=timedelta(
            minutes=resolved_settings.stale_after_minutes
        ),
    )
    scheduler = create_scheduler(
        sync_service,
        analytics_service,
        resolved_settings.timezone,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        scheduler_started = False
        try:
            scheduler.start()
            scheduler_started = True
            recent_job = scheduler.get_job("sync_recent")
            if recent_job is None:
                raise RuntimeError("Recent synchronization job is missing")
            scheduler.add_job(
                recent_job.func,
                "date",
                run_date=datetime.now(
                    ZoneInfo(resolved_settings.timezone)
                ),
                id="startup_sync",
                replace_existing=True,
                misfire_grace_time=900,
            )
            yield
        finally:
            try:
                if scheduler_started:
                    scheduler.shutdown(wait=True)
            finally:
                try:
                    property_http.close()
                finally:
                    wechat_http.close()

    application = FastAPI(lifespan=lifespan)
    application.add_middleware(
        SessionMiddleware,
        secret_key=resolved_settings.session_secret.get_secret_value(),
        https_only=True,
        same_site="lax",
        max_age=resolved_settings.session_max_age_seconds,
    )
    application.mount(
        "/static",
        StaticFiles(directory=PACKAGE_DIRECTORY / "static"),
        name="static",
    )
    application.include_router(
        create_router(
            resolved_settings,
            database,
            analytics_service,
            wechat_http,
        )
    )

    application.state.settings = resolved_settings
    application.state.database = database
    application.state.property_http = property_http
    application.state.property_client = property_client
    application.state.wechat_http = wechat_http
    application.state.sync_service = sync_service
    application.state.analytics_service = analytics_service
    application.state.scheduler = scheduler
    application.state.templates = TEMPLATES
    return application


def install_sensitive_log_filter() -> None:
    """Install one redaction filter on configured loggers and handlers."""
    _install_make_record_filter()
    log_filter = SensitiveDataFilter()
    root_logger = logging.getLogger()
    _add_filter_once(root_logger, log_filter)
    for handler in root_logger.handlers:
        _add_filter_once(handler, log_filter)
    for logger_value in logging.Logger.manager.loggerDict.values():
        if not isinstance(logger_value, logging.Logger):
            continue
        _add_filter_once(logger_value, log_filter)
        for handler in logger_value.handlers:
            _add_filter_once(handler, log_filter)


def _add_filter_once(
    target: logging.Logger | logging.Handler,
    log_filter: SensitiveDataFilter,
) -> None:
    if not any(
        getattr(existing, "_electricity_app_sensitive_data", False)
        for existing in target.filters
    ):
        target.addFilter(log_filter)


def _install_make_record_filter() -> None:
    current_make_record = logging.Logger.makeRecord
    if getattr(
        current_make_record,
        "_electricity_app_sensitive_data",
        False,
    ):
        return

    def make_record(
        logger: logging.Logger,
        *args: Any,
        **kwargs: Any,
    ) -> logging.LogRecord:
        record = current_make_record(logger, *args, **kwargs)
        SensitiveDataFilter().filter(record)
        return record

    make_record._electricity_app_sensitive_data = True  # type: ignore[attr-defined]
    logging.Logger.makeRecord = make_record  # type: ignore[method-assign]


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: (
                "[redacted]"
                if _SENSITIVE_KEY.search(str(key))
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


app = create_app()
