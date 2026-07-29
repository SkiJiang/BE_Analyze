from __future__ import annotations

import httpx
import json

from electricity_app.config import Settings
from electricity_app.wechat_template import WeChatTemplateClient


def test_template_client_posts_the_configured_template_payload(
    settings: Settings,
):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/cgi-bin/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = WeChatTemplateClient(settings, http=http)
        client.send_template(
            "openid-value",
            {
                "room": "7号楼/8F/805",
                "request_url": "https://electricity.example.test/wechat/entry",
            },
        )

    assert len(requests) == 2
    assert requests[0].url.path == "/cgi-bin/token"
    payload = json.loads(requests[1].content)
    assert payload["touser"] == "openid-value"
    assert payload["template_id"] == settings.wechat_daily_template_id
    assert payload["url"] == "https://electricity.example.test/wechat/entry"
    assert payload["data"]["request_url"]["value"].endswith("/wechat/entry")
