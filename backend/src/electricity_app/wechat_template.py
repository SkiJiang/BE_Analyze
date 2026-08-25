from __future__ import annotations

import time
from typing import Mapping

import httpx

from electricity_app.api_transport import (
    ExternalRequestError,
    json_object,
    request_with_retry,
)
from electricity_app.config import Settings


TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
TEMPLATE_SEND_URL = "https://api.weixin.qq.com/cgi-bin/message/template/send"


class WeChatTemplateClient:
    def __init__(self, settings: Settings, *, http: httpx.Client) -> None:
        self._settings = settings
        self._http = http
        self._access_token: str | None = None
        self._expires_at = 0.0

    def send_template(self, openid: str, values: Mapping[str, str]) -> None:
        token = self._token()
        request_url = values["request_url"]
        payload = {
            "touser": openid,
            "template_id": self._settings.wechat_daily_template_id,
            "url": request_url,
            "data": {
                key: {"value": value}
                for key, value in values.items()
            },
        }
        result = self._request_json(
            "post",
            TEMPLATE_SEND_URL,
            params={"access_token": token},
            json=payload,
            error_message="WeChat template request failed",
        )
        if not isinstance(result, dict) or result.get("errcode") != 0:
            raise RuntimeError("WeChat template request failed")

    def _token(self) -> str:
        if self._access_token is not None and time.monotonic() < self._expires_at:
            return self._access_token
        payload = self._request_json(
            "get",
            TOKEN_URL,
            params={
                "grant_type": "client_credential",
                "appid": self._settings.wechat_app_id,
                "secret": self._settings.wechat_app_secret.get_secret_value(),
            },
            error_message="WeChat token request failed",
        )
        if payload.get("errcode", 0) != 0:
            raise RuntimeError("WeChat token request failed")
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
        ):
            raise RuntimeError("WeChat token request failed")
        self._access_token = token
        self._expires_at = time.monotonic() + max(expires_in - 60, 0)
        return token

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        error_message: str,
        **kwargs: object,
    ) -> dict[str, object]:
        try:
            response = request_with_retry(
                self._http,
                method,
                url,
                **kwargs,
            )
            if response.status_code >= 400:
                raise RuntimeError(error_message)
            return json_object(response, error_message=error_message)
        except (ExternalRequestError, httpx.HTTPError, ValueError) as error:
            raise RuntimeError(error_message) from error
