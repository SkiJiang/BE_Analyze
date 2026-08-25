from __future__ import annotations

import httpx

from electricity_app.api_transport import request_with_retry


def test_request_with_retry_retries_server_errors_without_retrying_client_errors():
    attempts: list[str] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.method)
        if len(attempts) < 3:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = request_with_retry(
            client,
            "get",
            "https://example.test/health",
            sleep=sleeps.append,
        )

    assert response.status_code == 200
    assert attempts == ["GET", "GET", "GET"]
    assert sleeps == [0.25, 0.5]


def test_request_with_retry_does_not_retry_4xx():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = request_with_retry(
            client,
            "get",
            "https://example.test/health",
            sleep=lambda _: None,
        )

    assert response.status_code == 401
    assert attempts == 1
