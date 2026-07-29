from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import logging
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
import httpx
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from cryptography.fernet import Fernet

from electricity_app.analytics import AnalyticsService
from electricity_app.config import Settings
from electricity_app.db import Database


WECHAT_OAUTH_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
WECHAT_AUTHORIZE_URL = "https://open.weixin.qq.com/connect/oauth2/authorize"
OAUTH_STATE_MAX_AGE_SECONDS = 300
SHANGHAI = ZoneInfo("Asia/Shanghai")
PACKAGE_DIRECTORY = Path(__file__).resolve().parent
class _HttpxQueryRedactionFilter(logging.Filter):
    _electricity_app_query_redaction = True

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "httpx" or not isinstance(record.args, tuple):
            return True
        redacted_args: list[Any] = []
        for argument in record.args:
            if isinstance(argument, httpx.URL) and argument.query:
                redacted_args.append(argument.copy_with(query=None))
            else:
                redacted_args.append(argument)
        record.args = tuple(redacted_args)
        return True


class _UvicornCallbackQueryRedactionFilter(logging.Filter):
    _electricity_app_callback_query_redaction = True

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name != "uvicorn.access"
            or not isinstance(record.args, tuple)
            or len(record.args) < 3
        ):
            return True
        request_target = record.args[2]
        if not isinstance(request_target, str):
            return True
        path, separator, _ = request_target.partition("?")
        if separator and path.endswith(("/wechat/callback", "/wechat/message")):
            redacted_args = list(record.args)
            redacted_args[2] = path
            record.args = tuple(redacted_args)
        return True


def openid_hmac(openid: str, key: bytes) -> str:
    return hmac.new(key, openid.encode("utf-8"), hashlib.sha256).hexdigest()


def wechat_message_signature(token: str, timestamp: str, nonce: str) -> str:
    material = "".join(sorted((token, timestamp, nonce))).encode("utf-8")
    return hashlib.sha1(material).hexdigest()


def create_router(
    settings: Settings,
    db: Database,
    analytics: AnalyticsService,
    wechat_http: httpx.Client,
) -> APIRouter:
    _install_privacy_log_filters()
    router = APIRouter()
    state_signer = URLSafeTimedSerializer(
        settings.session_secret.get_secret_value(),
        salt="wechat-oauth-state",
    )
    hmac_key = settings.openid_hmac_key.get_secret_value().encode("utf-8")
    callback_url = (
        f"{str(settings.public_base_url).rstrip('/')}/wechat/callback"
    )

    def require_authorized_identity(request: Request) -> str:
        identity = request.session.get("openid_hmac")
        if not isinstance(identity, str) or not db.is_openid_allowed(identity):
            request.session.pop("openid_hmac", None)
            raise HTTPException(status_code=401, detail="unauthorized")
        return identity

    @router.get("/wechat/message")
    def wechat_message_verification(
        signature: str | None = None,
        timestamp: str | None = None,
        nonce: str | None = None,
        echostr: str | None = None,
    ) -> Response:
        if not all((signature, timestamp, nonce, echostr)):
            raise HTTPException(status_code=403, detail="forbidden")
        expected = wechat_message_signature(
            settings.wechat_message_token.get_secret_value(),
            timestamp,
            nonce,
        )
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=403, detail="forbidden")
        return Response(content=echostr, media_type="text/plain")

    @router.post("/wechat/message")
    def wechat_message_receive() -> Response:
        return Response(status_code=200)

    @router.get("/wechat/entry")
    def wechat_entry(request: Request) -> RedirectResponse:
        nonce = secrets.token_urlsafe(32)
        issued_at = time.time()
        nonce_digest = _oauth_nonce_digest(nonce)
        issued_instant = datetime.fromtimestamp(issued_at, timezone.utc)
        state = state_signer.dumps(
            {"nonce": nonce, "issued_at": issued_at}
        )
        db.create_oauth_nonce(
            nonce_digest,
            created_at=issued_instant,
            expires_at=issued_instant
            + timedelta(seconds=OAUTH_STATE_MAX_AGE_SECONDS),
        )
        request.session["wechat_oauth_nonce_digest"] = nonce_digest
        query = urlencode(
            {
                "appid": settings.wechat_app_id,
                "redirect_uri": callback_url,
                "response_type": "code",
                "scope": "snsapi_userinfo",
                "state": state,
            }
        )
        return RedirectResponse(
            f"{WECHAT_AUTHORIZE_URL}?{query}#wechat_redirect",
            status_code=307,
        )

    @router.get("/wechat/callback", response_model=None)
    def wechat_callback(
        request: Request,
        code: str,
        state: str,
    ) -> JSONResponse | RedirectResponse:
        expected_nonce_digest = request.session.pop(
            "wechat_oauth_nonce_digest", None
        )
        try:
            payload = state_signer.loads(
                state, max_age=OAUTH_STATE_MAX_AGE_SECONDS
            )
            nonce, issued_at = _validated_state(payload)
            consumed_timestamp = time.time()
            age = consumed_timestamp - issued_at
            if age < 0 or age > OAUTH_STATE_MAX_AGE_SECONDS:
                raise BadSignature("invalid state time")
            nonce_digest = _oauth_nonce_digest(nonce)
            if (
                not isinstance(expected_nonce_digest, str)
                or not hmac.compare_digest(
                    nonce_digest, expected_nonce_digest
                )
            ):
                raise BadSignature("invalid state nonce")
            if not db.consume_oauth_nonce(
                nonce_digest,
                consumed_at=datetime.fromtimestamp(
                    consumed_timestamp, timezone.utc
                ),
            ):
                raise BadSignature("OAuth state already consumed")
        except (BadSignature, SignatureExpired, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid OAuth state")

        oauth_payload = _exchange_wechat_code(
            wechat_http=wechat_http,
            settings=settings,
            code=code,
        )
        raw_openid = oauth_payload.get("openid")
        if not isinstance(raw_openid, str) or not raw_openid:
            raise HTTPException(
                status_code=502, detail="WeChat authorization failed"
            )

        identity = openid_hmac(raw_openid, hmac_key)
        if not db.is_openid_allowed(identity):
            request_id = db.upsert_pending_openid(identity)
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "authorization pending",
                    "request_id": request_id,
                },
            )

        encrypted_openid = Fernet(
            settings.wechat_openid_encryption_key.get_secret_value().encode(
                "utf-8"
            )
        ).encrypt(raw_openid.encode("utf-8")).decode("utf-8")
        if not db.save_authorized_openid(identity, encrypted_openid):
            raise HTTPException(status_code=403, detail="unauthorized")
        request.session.clear()
        request.session["openid_hmac"] = identity
        return RedirectResponse("/dashboard", status_code=307)

    @router.get("/dashboard", response_class=HTMLResponse)
    def dashboard_page(request: Request) -> Response:
        try:
            require_authorized_identity(request)
        except HTTPException:
            return RedirectResponse("/wechat/entry", status_code=303)
        return FileResponse(
            PACKAGE_DIRECTORY / "static" / "dashboard.html",
            media_type="text/html",
        )

    @router.get("/static/app.css", response_class=FileResponse)
    def dashboard_styles() -> FileResponse:
        return FileResponse(
            PACKAGE_DIRECTORY / "static" / "app.css",
            media_type="text/css",
        )

    @router.get("/static/app.js", response_class=FileResponse)
    def dashboard_script() -> FileResponse:
        return FileResponse(
            PACKAGE_DIRECTORY / "static" / "app.js",
            media_type="text/javascript",
        )

    @router.get("/static/dashboard.html", response_class=FileResponse)
    def dashboard_document() -> FileResponse:
        return FileResponse(
            PACKAGE_DIRECTORY / "static" / "dashboard.html",
            media_type="text/html",
        )

    @router.get(
        "/api/dashboard",
        dependencies=[Depends(require_authorized_identity)],
    )
    def dashboard() -> JSONResponse:
        summary = analytics.dashboard(datetime.now(SHANGHAI))
        return JSONResponse(content=jsonable_encoder(summary))

    @router.get(
        "/api/day/{day}",
        dependencies=[Depends(require_authorized_identity)],
    )
    def day_detail(day: date) -> JSONResponse:
        detail = analytics.day_detail(day)
        return JSONResponse(content=jsonable_encoder(detail))

    @router.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "live"}

    @router.get("/health/ready")
    def health_ready() -> JSONResponse:
        try:
            db.count_pending_openids()
            latest_status = db.latest_sync_status()
        except sqlite3.Error:
            return JSONResponse(
                status_code=503, content={"status": "not_ready"}
            )

        if latest_status == "auth_required":
            return JSONResponse(
                status_code=503, content={"status": "auth_required"}
            )

        if db.last_successful_sync() is not None:
            summary = analytics.dashboard(datetime.now(SHANGHAI))
            if summary.is_stale:
                return JSONResponse(content={"status": "degraded"})

        return JSONResponse(content={"status": "ready"})

    return router


def _validated_state(payload: Any) -> tuple[str, float]:
    if not isinstance(payload, dict):
        raise TypeError("invalid state payload")
    nonce = payload.get("nonce")
    issued_at = payload.get("issued_at")
    if not isinstance(nonce, str) or not nonce:
        raise TypeError("invalid state nonce")
    if not isinstance(issued_at, (int, float)) or isinstance(issued_at, bool):
        raise TypeError("invalid state time")
    return nonce, float(issued_at)


def _oauth_nonce_digest(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def _install_privacy_log_filters() -> None:
    httpx_logger = logging.getLogger("httpx")
    if not any(
        getattr(log_filter, "_electricity_app_query_redaction", False)
        for log_filter in httpx_logger.filters
    ):
        httpx_logger.addFilter(_HttpxQueryRedactionFilter())

    access_logger = logging.getLogger("uvicorn.access")
    if not any(
        getattr(
            log_filter,
            "_electricity_app_callback_query_redaction",
            False,
        )
        for log_filter in access_logger.filters
    ):
        access_logger.addFilter(_UvicornCallbackQueryRedactionFilter())


def _exchange_wechat_code(
    *,
    wechat_http: httpx.Client,
    settings: Settings,
    code: str,
) -> dict[str, Any]:
    try:
        response = wechat_http.get(
            WECHAT_OAUTH_URL,
            params={
                "appid": settings.wechat_app_id,
                "secret": settings.wechat_app_secret.get_secret_value(),
                "code": code,
                "grant_type": "authorization_code",
            },
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(
            status_code=502, detail="WeChat authorization failed"
        )
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=502, detail="WeChat authorization failed"
        )
    return payload
