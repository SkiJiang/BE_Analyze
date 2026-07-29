from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import hashlib
import logging
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from starlette.middleware.sessions import SessionMiddleware

from electricity_app.analytics import AnalyticsService
from electricity_app.config import Settings
from electricity_app.db import Database
from electricity_app.domain import SyncOutcome
from electricity_app.web import create_router, openid_hmac


SHANGHAI = ZoneInfo("Asia/Shanghai")
RAW_OPENID = "openid-value"
ACCESS_TOKEN = "access-token-value"
VALID_CODE = "valid-code"


class WeChatOAuth:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": ACCESS_TOKEN,
                "expires_in": 7200,
                "refresh_token": "refresh-token-value",
                "openid": RAW_OPENID,
                "scope": "snsapi_userinfo",
            },
        )


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.database_path)
    database.initialize()
    return database


@pytest.fixture
def wechat_oauth_mock() -> WeChatOAuth:
    return WeChatOAuth()


def make_client(
    settings: Settings,
    db: Database,
    wechat_oauth_mock: WeChatOAuth,
) -> TestClient:
    wechat_http = httpx.Client(transport=httpx.MockTransport(wechat_oauth_mock))
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        https_only=True,
        same_site="lax",
    )
    app.include_router(
        create_router(settings, db, AnalyticsService(db), wechat_http)
    )
    return TestClient(app, base_url="https://electricity.example.test")


@pytest.fixture
def client(
    settings: Settings,
    db: Database,
    wechat_oauth_mock: WeChatOAuth,
) -> TestClient:
    with make_client(settings, db, wechat_oauth_mock) as test_client:
        yield test_client


def begin_oauth(client: TestClient) -> str:
    response = client.get("/wechat/entry", follow_redirects=False)
    assert response.status_code == 307
    state = parse_qs(urlparse(response.headers["location"]).query)["state"]
    return state[0]


def complete_oauth(client: TestClient, state: str) -> httpx.Response:
    return client.get(
        "/wechat/callback",
        params={"code": VALID_CODE, "state": state},
        follow_redirects=False,
    )


def _message_signature(settings: Settings, timestamp: str, nonce: str) -> str:
    material = "".join(
        sorted((settings.wechat_message_token.get_secret_value(), timestamp, nonce))
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


def test_wechat_message_verification_echoes_echostr_for_valid_signature(
    client: TestClient,
    settings: Settings,
):
    timestamp = "1710000000"
    nonce = "nonce-value"
    response = client.get(
        "/wechat/message",
        params={
            "signature": _message_signature(settings, timestamp, nonce),
            "timestamp": timestamp,
            "nonce": nonce,
            "echostr": "wechat-challenge",
        },
    )

    assert response.status_code == 200
    assert response.text == "wechat-challenge"


def test_wechat_message_verification_rejects_invalid_or_missing_signature(
    client: TestClient,
):
    invalid = client.get(
        "/wechat/message",
        params={
            "signature": "wrong",
            "timestamp": "1",
            "nonce": "n",
            "echostr": "e",
        },
    )
    missing = client.get("/wechat/message")

    assert invalid.status_code == 403
    assert missing.status_code == 403


def test_wechat_message_post_returns_empty_success(client: TestClient):
    response = client.post("/wechat/message", content=b"<xml>private</xml>")

    assert response.status_code == 200
    assert response.content == b""


@pytest.fixture
def authorized_client(
    client: TestClient,
    settings: Settings,
    db: Database,
) -> TestClient:
    digest = openid_hmac(
        RAW_OPENID, settings.openid_hmac_key.get_secret_value().encode("utf-8")
    )
    request_id = db.upsert_pending_openid(digest)
    assert db.set_openid_enabled(request_id, True)
    response = complete_oauth(client, begin_oauth(client))
    assert response.status_code == 307
    return client


def record_sync(db: Database, *, status: str, finished_at: datetime) -> None:
    outcome = SyncOutcome(
        started_at=finished_at - timedelta(minutes=1),
        finished_at=finished_at,
        start_date=finished_at.date(),
        end_date=finished_at.date(),
        status=status,
        fetched=0,
        inserted=0,
        updated=0,
        error_code="PropertyAuthenticationError"
        if status == "auth_required"
        else None,
    )
    db.apply_sync([], outcome, finished_at)


def test_openid_hmac_uses_sha256_with_the_configured_key():
    digest = openid_hmac(RAW_OPENID, b"h" * 32)

    assert digest == (
        "26d9bf77952bdcdd26d8e056b9085119"
        "ba57247f139c0d7357465bf8194ca3e1"
    )


def test_wechat_entry_redirects_with_signed_state(client: TestClient):
    response = client.get("/wechat/entry", follow_redirects=False)

    assert response.status_code == 307
    location = response.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://open.weixin.qq.com/connect/oauth2/authorize"
    )
    assert query["scope"] == ["snsapi_userinfo"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [
        "https://electricity.example.test/wechat/callback"
    ]
    assert query["state"][0]


def test_callback_creates_pending_request_for_unknown_openid(
    client: TestClient,
    wechat_oauth_mock: WeChatOAuth,
    db: Database,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.DEBUG)

    response = complete_oauth(client, begin_oauth(client))

    assert response.status_code == 403
    assert db.count_pending_openids() == 1
    assert response.json() == {
        "detail": "authorization pending",
        "request_id": 1,
    }
    exposed = response.text + " ".join(record.getMessage() for record in caplog.records)
    assert RAW_OPENID not in exposed
    assert ACCESS_TOKEN not in exposed
    assert VALID_CODE not in exposed


def test_callback_exchanges_code_only_at_wechat_oauth_endpoint(
    client: TestClient,
    settings: Settings,
    wechat_oauth_mock: WeChatOAuth,
):
    complete_oauth(client, begin_oauth(client))

    assert len(wechat_oauth_mock.requests) == 1
    request = wechat_oauth_mock.requests[0]
    assert request.url.copy_with(query=None) == httpx.URL(
        "https://api.weixin.qq.com/sns/oauth2/access_token"
    )
    assert dict(request.url.params) == {
        "appid": settings.wechat_app_id,
        "secret": settings.wechat_app_secret.get_secret_value(),
        "code": VALID_CODE,
        "grant_type": "authorization_code",
    }


def test_callback_redirects_enabled_openid_to_dashboard(
    client: TestClient,
    settings: Settings,
    db: Database,
):
    digest = openid_hmac(
        RAW_OPENID,
        settings.openid_hmac_key.get_secret_value().encode("utf-8"),
    )
    request_id = db.upsert_pending_openid(digest)
    assert db.set_openid_enabled(request_id, True)

    response = complete_oauth(client, begin_oauth(client))

    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard"
    recipients = db.list_reminder_recipients()
    assert len(recipients) == 1
    assert recipients[0][0] == digest
    assert recipients[0][1] != RAW_OPENID


def test_callback_rejects_expired_state_before_code_exchange(
    client: TestClient,
    wechat_oauth_mock: WeChatOAuth,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    issued_at = 1_785_299_400
    monkeypatch.setattr("time.time", lambda: issued_at)
    state = begin_oauth(client)
    monkeypatch.setattr("time.time", lambda: issued_at + 301)

    response = complete_oauth(client, state)

    assert response.status_code == 400
    assert wechat_oauth_mock.requests == []
    assert db.count_pending_openids() == 0


def test_callback_accepts_state_at_exactly_300_seconds(
    client: TestClient,
    wechat_oauth_mock: WeChatOAuth,
    monkeypatch: pytest.MonkeyPatch,
):
    issued_at = 1_785_299_400.25
    monkeypatch.setattr("time.time", lambda: issued_at)
    state = begin_oauth(client)
    monkeypatch.setattr("time.time", lambda: issued_at + 300)

    response = complete_oauth(client, state)

    assert response.status_code == 403
    assert len(wechat_oauth_mock.requests) == 1


def test_callback_rejects_state_one_millisecond_after_300_seconds(
    client: TestClient,
    wechat_oauth_mock: WeChatOAuth,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    issued_at = 1_785_299_400.25
    monkeypatch.setattr("time.time", lambda: issued_at)
    state = begin_oauth(client)
    monkeypatch.setattr("time.time", lambda: issued_at + 300.001)

    response = complete_oauth(client, state)

    assert response.status_code == 400
    assert wechat_oauth_mock.requests == []
    assert db.count_pending_openids() == 0


def test_callback_rejects_state_replaced_by_a_new_login_attempt(
    client: TestClient,
    wechat_oauth_mock: WeChatOAuth,
):
    replaced_state = begin_oauth(client)
    begin_oauth(client)

    response = complete_oauth(client, replaced_state)

    assert response.status_code == 400
    assert wechat_oauth_mock.requests == []


def test_callback_rejects_replay_with_the_original_signed_session_cookie(
    client: TestClient,
    settings: Settings,
    db: Database,
    wechat_oauth_mock: WeChatOAuth,
):
    state = begin_oauth(client)
    original_session_cookie = client.cookies["session"]
    first_response = complete_oauth(client, state)

    with make_client(settings, db, wechat_oauth_mock) as replay_client:
        replay_client.cookies.set("session", original_session_cookie)
        replay_response = complete_oauth(replay_client, state)

    assert first_response.status_code == 403
    assert replay_response.status_code == 400
    assert len(wechat_oauth_mock.requests) == 1


def test_only_one_concurrent_callback_can_consume_the_same_oauth_nonce(
    client: TestClient,
    settings: Settings,
    db: Database,
    wechat_oauth_mock: WeChatOAuth,
):
    state = begin_oauth(client)
    original_session_cookie = client.cookies["session"]

    with (
        make_client(settings, db, wechat_oauth_mock) as first_client,
        make_client(settings, db, wechat_oauth_mock) as second_client,
    ):
        first_client.cookies.set("session", original_session_cookie)
        second_client.cookies.set("session", original_session_cookie)
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(
                executor.map(
                    lambda callback_client: complete_oauth(callback_client, state),
                    (first_client, second_client),
                )
            )

    assert sorted(response.status_code for response in responses) == [400, 403]
    assert len(wechat_oauth_mock.requests) == 1


def test_uvicorn_access_log_redacts_callback_query_values():
    from electricity_app.web import _UvicornCallbackQueryRedactionFilter

    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "127.0.0.1:12345",
            "GET",
            "/wechat/callback?code=private-code&state=private-state",
            "1.1",
            400,
        ),
        exc_info=None,
    )

    assert _UvicornCallbackQueryRedactionFilter().filter(record) is True
    message = record.getMessage()
    assert 'GET /wechat/callback HTTP/1.1" 400' in message
    assert "private-code" not in message
    assert "private-state" not in message


def test_uvicorn_access_log_redacts_message_query_values():
    from electricity_app.web import _UvicornCallbackQueryRedactionFilter

    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "127.0.0.1:12345",
            "GET",
            "/wechat/message?signature=private&echostr=challenge",
            "1.1",
            200,
        ),
        exc_info=None,
    )

    assert _UvicornCallbackQueryRedactionFilter().filter(record) is True
    assert record.args[2] == "/wechat/message"


def test_dashboard_api_rejects_session_without_enabled_allowlist(
    client: TestClient,
):
    response = client.get("/api/dashboard")

    assert response.status_code == 401


def test_dashboard_api_allows_enabled_openid(authorized_client: TestClient):
    response = authorized_client.get("/api/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert "today_energy" in payload
    assert set(payload) >= {
        "range_24h",
        "range_7d",
        "range_30d",
        "recent_seven_day_mean_energy",
        "recent_seven_day_change_percent",
        "typical_historical_peak_hour",
    }
    assert len(payload["range_24h"]["points"]) == 48
    assert len(payload["range_7d"]["points"]) == 7
    assert len(payload["range_30d"]["points"]) == 30
    assert RAW_OPENID not in authorized_client.cookies.get("session", "")


def test_dashboard_api_rechecks_allowlist_after_session_is_revoked(
    authorized_client: TestClient,
    settings: Settings,
    db: Database,
):
    digest = openid_hmac(
        RAW_OPENID, settings.openid_hmac_key.get_secret_value().encode("utf-8")
    )
    request_id = db.upsert_pending_openid(digest)
    assert db.set_openid_enabled(request_id, False)

    response = authorized_client.get("/api/dashboard")

    assert response.status_code == 401


def test_dashboard_page_contains_required_sections(
    authorized_client: TestClient,
):
    response = authorized_client.get("/dashboard")

    assert response.status_code == 200
    assert 'id="balance-card"' in response.text
    assert 'id="trend-chart"' in response.text
    assert 'id="hourly-chart"' in response.text
    assert 'id="anomaly-list"' in response.text
    assert 'id="stale-banner"' in response.text


def test_static_dashboard_asset_contains_required_sections(
    client: TestClient,
):
    response = client.get("/static/dashboard.html")

    assert response.status_code == 200
    assert 'id="balance-card"' in response.text
    assert 'id="trend-chart"' in response.text
    assert 'id="hourly-chart"' in response.text


def test_dashboard_page_redirects_unauthenticated_user(client: TestClient):
    response = client.get("/dashboard", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/wechat/entry"


def test_day_api_requires_authorization(client: TestClient):
    response = client.get("/api/day/2026-07-29")

    assert response.status_code == 401


def test_invalid_day_does_not_expose_validation_before_authorization(
    client: TestClient,
):
    response = client.get("/api/day/not-a-date")

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


def test_day_api_returns_requested_day(authorized_client: TestClient):
    response = authorized_client.get("/api/day/2026-07-29")

    assert response.status_code == 200
    assert response.json()["day"] == "2026-07-29"


def test_health_live_succeeds_before_database_initialization(
    settings: Settings,
    wechat_oauth_mock: WeChatOAuth,
):
    uninitialized_db = Database(settings.database_path.parent / "missing.db")
    with make_client(settings, uninitialized_db, wechat_oauth_mock) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live"}


def test_health_ready_after_database_initialization(client: TestClient):
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_health_ready_uses_only_public_database_status_methods(
    settings: Settings,
    wechat_oauth_mock: WeChatOAuth,
):
    class PublicDatabaseBoundary:
        def count_pending_openids(self) -> int:
            return 0

        def latest_sync_status(self) -> str | None:
            return None

        def last_successful_sync(self) -> None:
            return None

    public_db = PublicDatabaseBoundary()
    with make_client(settings, public_db, wechat_oauth_mock) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_health_ready_reports_exhausted_property_authentication(
    client: TestClient,
    db: Database,
):
    record_sync(
        db,
        status="auth_required",
        finished_at=datetime.now(SHANGHAI),
    )

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "auth_required"}


def test_health_ready_reports_stale_historical_data_as_degraded(
    client: TestClient,
    db: Database,
):
    record_sync(
        db,
        status="success",
        finished_at=datetime.now(SHANGHAI) - timedelta(minutes=91),
    )

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "degraded"}


def test_health_ready_reports_recent_success_as_ready(
    client: TestClient,
    db: Database,
):
    record_sync(
        db,
        status="success",
        finished_at=datetime.now(SHANGHAI),
    )

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
