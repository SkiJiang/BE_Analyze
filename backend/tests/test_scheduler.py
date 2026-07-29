from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
import importlib
from io import StringIO
import logging
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from electricity_app.domain import SyncOutcome
from electricity_app.db import Database
from electricity_app.scheduler import create_scheduler


SHANGHAI = ZoneInfo("Asia/Shanghai")


def successful_outcome(start_date: date, end_date: date) -> SyncOutcome:
    finished_at = datetime(2026, 7, 29, 10, 30, tzinfo=SHANGHAI)
    return SyncOutcome(
        started_at=finished_at,
        finished_at=finished_at,
        start_date=start_date,
        end_date=end_date,
        status="success",
        fetched=0,
        inserted=0,
        updated=0,
    )


def test_scheduler_has_30_minute_sync_and_daily_reconcile(
    sync_service, analytics_service
):
    scheduler = create_scheduler(
        sync_service, analytics_service, "Asia/Shanghai"
    )

    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert set(jobs) == {"sync_recent", "reconcile_30_days"}
    assert "minute='0,30'" in str(jobs["sync_recent"].trigger)
    assert "hour='2'" in str(jobs["reconcile_30_days"].trigger)
    assert "minute='15'" in str(jobs["reconcile_30_days"].trigger)
    assert jobs["sync_recent"].max_instances == 1
    assert jobs["sync_recent"].coalesce is True
    assert jobs["sync_recent"].misfire_grace_time == 900
    assert jobs["reconcile_30_days"].max_instances == 1
    assert jobs["reconcile_30_days"].coalesce is True
    assert jobs["reconcile_30_days"].misfire_grace_time == 900


def test_successful_job_rebuilds_the_synchronized_date_range(
    sync_service: Mock, analytics_service: Mock
):
    outcome = successful_outcome(date(2026, 7, 28), date(2026, 7, 29))
    sync_service.sync_recent_now.return_value = outcome
    scheduler = create_scheduler(
        sync_service, analytics_service, "Asia/Shanghai"
    )

    scheduler.get_job("sync_recent").func()

    analytics_service.rebuild_daily_summaries.assert_called_once_with(
        outcome.start_date,
        outcome.end_date,
        outcome.finished_at,
    )


def test_failed_job_does_not_rebuild_summaries(
    sync_service: Mock, analytics_service: Mock
):
    outcome = successful_outcome(date(2026, 7, 28), date(2026, 7, 29))
    sync_service.sync_recent_now.return_value = replace(
        outcome, status="failed"
    )
    scheduler = create_scheduler(
        sync_service, analytics_service, "Asia/Shanghai"
    )

    scheduler.get_job("sync_recent").func()

    analytics_service.rebuild_daily_summaries.assert_not_called()


def test_recent_sync_and_reconciliation_cannot_overlap(
    sync_service: Mock, analytics_service: Mock
):
    recent_outcome = successful_outcome(
        date(2026, 7, 28), date(2026, 7, 29)
    )
    reconcile_outcome = successful_outcome(
        date(2026, 6, 30), date(2026, 7, 29)
    )
    sync_service.reconcile_30_days_now.return_value = reconcile_outcome
    scheduler = create_scheduler(
        sync_service, analytics_service, "Asia/Shanghai"
    )
    reconcile_job = scheduler.get_job("reconcile_30_days")

    def run_reconcile_while_recent_holds_lock() -> SyncOutcome:
        reconcile_job.func()
        return recent_outcome

    sync_service.sync_recent_now.side_effect = run_reconcile_while_recent_holds_lock

    scheduler.get_job("sync_recent").func()

    sync_service.reconcile_30_days_now.assert_not_called()
    analytics_service.rebuild_daily_summaries.assert_called_once_with(
        recent_outcome.start_date,
        recent_outcome.end_date,
        recent_outcome.finished_at,
    )


def import_main(monkeypatch, settings):
    environment = {
        "PROPERTY_BASE_URL": str(settings.property_base_url),
        "PROPERTY_USERNAME": settings.property_username,
        "PROPERTY_PASSWORD": settings.property_password.get_secret_value(),
        "DATABASE_PATH": str(settings.database_path),
        "SESSION_SECRET": settings.session_secret.get_secret_value(),
        "OPENID_HMAC_KEY": settings.openid_hmac_key.get_secret_value(),
        "WECHAT_APP_ID": settings.wechat_app_id,
        "WECHAT_APP_SECRET": settings.wechat_app_secret.get_secret_value(),
        "WECHAT_MESSAGE_TOKEN": settings.wechat_message_token.get_secret_value(),
        "PUBLIC_BASE_URL": str(settings.public_base_url),
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    return importlib.import_module("electricity_app.main")


def close_app_clients(app) -> None:
    app.state.property_http.close()
    app.state.wechat_http.close()


def test_create_app_initializes_database_static_routes_and_secure_session(
    settings, monkeypatch
):
    main = import_main(monkeypatch, settings)
    close_app_clients(main.app)

    app = main.create_app(settings)

    try:
        assert settings.database_path.exists()
        routes = {route.path for route in app.routes}
        assert {
            "/static/app.css",
            "/static/app.js",
            "/static/dashboard.html",
        } <= routes
        session = next(
            middleware
            for middleware in app.user_middleware
            if middleware.cls is SessionMiddleware
        )
        assert session.kwargs["https_only"] is True
        assert session.kwargs["same_site"] == "lax"
        assert session.kwargs["max_age"] == 1800
        assert (
            session.kwargs["secret_key"]
            == settings.session_secret.get_secret_value()
        )
    finally:
        close_app_clients(app)


def test_application_restart_clears_property_auth_gate(settings, monkeypatch):
    main = import_main(monkeypatch, settings)
    close_app_clients(main.app)
    database = Database(settings.database_path)
    database.initialize()
    finished_at = datetime(2026, 7, 29, 10, 30, tzinfo=SHANGHAI)
    database.apply_sync(
        [],
        replace(
            successful_outcome(date(2026, 7, 29), date(2026, 7, 29)),
            status="auth_required",
            error_code="authentication",
        ),
        finished_at,
    )
    assert database.auth_gate_active() is True

    app = main.create_app(settings)

    try:
        assert app.state.database.auth_gate_active() is False
    finally:
        close_app_clients(app)


def test_lifespan_starts_scheduler_queues_sync_and_closes_resources(
    settings, monkeypatch
):
    main = import_main(monkeypatch, settings)
    close_app_clients(main.app)
    clients = []

    class FakeHttpClient:
        def __init__(self, *args, **kwargs):
            self.closed = False
            clients.append(self)

        def close(self):
            self.closed = True

    class FakeScheduler:
        def __init__(self):
            self.events = []
            self.running = False
            self.recent_job = SimpleNamespace(func=Mock(name="recent_job"))

        def start(self):
            self.events.append("start")
            self.running = True

        def get_job(self, job_id):
            assert job_id == "sync_recent"
            return self.recent_job

        def add_job(self, func, trigger, **kwargs):
            self.events.append(("add_job", func, trigger, kwargs))

        def shutdown(self, *, wait):
            self.events.append(("shutdown", wait))
            self.running = False

    scheduler = FakeScheduler()
    monkeypatch.setattr(main.httpx, "Client", FakeHttpClient)
    monkeypatch.setattr(main, "PropertyClient", Mock())
    monkeypatch.setattr(main, "create_router", lambda *args: APIRouter())
    monkeypatch.setattr(main, "create_scheduler", lambda *args: scheduler)

    app = main.create_app(settings)

    with TestClient(app):
        assert scheduler.events[0] == "start"
        queued = scheduler.events[1]
        assert queued[0] == "add_job"
        assert queued[1] is scheduler.recent_job.func
        assert queued[2] == "date"
        assert scheduler.recent_job.func.call_count == 0

    assert scheduler.events[-1] == ("shutdown", True)
    assert len(clients) == 2
    assert all(client.closed for client in clients)


def test_sensitive_log_filter_redacts_nested_matching_keys(
    settings, monkeypatch
):
    main = import_main(monkeypatch, settings)
    close_app_clients(main.app)
    record = logging.LogRecord(
        "electricity_app",
        logging.INFO,
        __file__,
        1,
        {
            "username": "safe",
            "password": "secret",
            "nested": {
                "access_token": "token-value",
                "OpenID": "openid-value",
                "authorization": "Bearer secret",
                "cookie": "session=value",
                "code": "oauth-code",
            },
        },
        (),
        None,
    )

    assert main.SensitiveDataFilter().filter(record) is True
    assert record.msg == {
        "username": "safe",
        "password": "[redacted]",
        "nested": {
            "access_token": "[redacted]",
            "OpenID": "[redacted]",
            "authorization": "[redacted]",
            "cookie": "[redacted]",
            "code": "[redacted]",
        },
    }


def test_sensitive_log_filter_covers_loggers_and_handlers_created_later(
    settings, monkeypatch
):
    main = import_main(monkeypatch, settings)
    close_app_clients(main.app)
    main.install_sensitive_log_filter()
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s %(access_token)s"))
    logger = logging.getLogger("electricity_app.late_logger")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    try:
        logger.info(
            {"password": "password-value"},
            extra={"access_token": "token-value"},
        )
    finally:
        logger.handlers.clear()
        logger.propagate = True

    assert stream.getvalue().strip() == (
        "{'password': '[redacted]'} [redacted]"
    )
