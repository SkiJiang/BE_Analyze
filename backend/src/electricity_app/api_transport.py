"""Shared, privacy-preserving transport helpers for external APIs."""

from __future__ import annotations

import ssl
import time
from collections.abc import Callable
from typing import Any

import httpx


class ExternalRequestError(RuntimeError):
    """A safe transport-level error from an external HTTP API."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    retry_base_seconds: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """Perform a bounded request without leaking URL query values or bodies."""
    normalized_method = method.lower()
    if normalized_method not in {"get", "post"}:
        raise ValueError("unsupported HTTP method")
    if attempts < 1:
        raise ValueError("attempts must be positive")

    request = getattr(client, normalized_method)
    for attempt in range(attempts):
        try:
            response = request(url, **kwargs)
        except httpx.RequestError as error:
            if attempt + 1 < attempts:
                sleep(retry_base_seconds * (2**attempt))
                continue
            raise ExternalRequestError(
                "External API transport failed",
                code="tls" if is_tls_error(error) else "network",
            ) from error

        if response.status_code < 500 or attempt + 1 >= attempts:
            return response
        sleep(retry_base_seconds * (2**attempt))

    raise AssertionError("unreachable")


def json_object(response: httpx.Response, *, error_message: str) -> dict[str, object]:
    """Decode an external response while enforcing an object-shaped payload."""
    try:
        payload: Any = response.json()
    except ValueError as error:
        raise ExternalRequestError(error_message, code="invalid_json") from error
    if not isinstance(payload, dict):
        raise ExternalRequestError(error_message, code="invalid_json")
    return payload


def is_tls_error(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False


def is_auth_payload(payload: dict[str, object]) -> bool:
    """Recognize common upstream authentication failures without logging values."""
    code = str(payload.get("code", "")).lower()
    if code in {"401", "403", "unauthorized", "forbidden", "token_expired"}:
        return True
    message = " ".join(
        str(payload.get(key, "")) for key in ("message", "msg", "error")
    ).lower()
    return any(
        marker in message
        for marker in (
            "token",
            "auth",
            "login",
            "登录",
            "令牌",
            "认证",
            "鉴权",
        )
    )
