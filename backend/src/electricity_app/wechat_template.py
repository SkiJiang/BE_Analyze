from __future__ import annotations

import time
from typing import Mapping

import httpx

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
        try:
            response = self._http.post(
                TEMPLATE_SEND_URL,
                params={"access_token": token},
                json=payload,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RuntimeError("WeChat template request failed") from error
        if not isinstance(result, dict) or result.get("errcode") != 0:
            raise RuntimeError("WeChat template request failed")

    def _token(self) -> str:
        if self._access_token is not None and time.monotonic() < self._expires_at:
            return self._access_token
        try:
            response = self._http.get(
                TOKEN_URL,
                params={
                    "grant_type": "client_credential",
                    "appid": self._settings.wechat_app_id,
                    "secret": self._settings.wechat_app_secret.get_secret_value(),
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RuntimeError("WeChat token request failed") from error
        if not isinstance(payload, dict):
            raise RuntimeError("WeChat token request failed")
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(token, str) or not token or not isinstance(expires_in, int):
            raise RuntimeError("WeChat token request failed")
        self._access_token = token
        self._expires_at = time.monotonic() + max(expires_in - 60, 0)
        return token
