from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import socket
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from threading import Event
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import pytest
from websockets.sync.client import connect


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src" / "electricity_app" / "static"
TEMPLATE = (
    ROOT
    / "src"
    / "electricity_app"
    / "templates"
    / "dashboard.html"
)
ECHARTS_TAG = (
    '<script defer src="https://cdn.jsdelivr.net/npm/'
    'echarts@5.4.3/dist/echarts.min.js"></script>'
)


def _range(key: str, days: int, energy: str, cost: str) -> dict:
    points = [
        {
            "label": f"2026-07-{30 - days + index:02d}",
            "energy": str(index + 1),
            "cost": str(index + 1),
        }
        for index in range(days)
    ]
    return {
        "key": key,
        "total_energy": energy,
        "total_cost": cost,
        "points": points,
        "highest_use_day": points[-1]["label"] if key != "24h" else None,
        "highest_use_day_energy": points[-1]["energy"]
        if key != "24h"
        else None,
    }


def dashboard_payload() -> dict:
    hourly = [
        {
            "start": f"2026-07-29T{hour:02d}:00:00+08:00",
            "energy": "0",
            "cost": "0",
        }
        for hour in range(24)
    ]
    recent = [
        {
            "start": (
                f"2026-07-{28 if index < 24 else 29:02d}T"
                f"{index % 24:02d}:00:00+08:00"
            ),
            "energy": "0.1",
            "cost": "0.05",
        }
        for index in range(48)
    ]
    return {
        "balance": "100.00",
        "today_energy": "4.20",
        "today_cost": "2.31",
        "yesterday_energy": "3.00",
        "day_change_percent": "40.0",
        "seven_day_energy": "17.20",
        "thirty_day_energy": "29.20",
        "daily_average_energy": "2.1",
        "peak_bucket": {
            "start": "2026-07-29T10:00:00+08:00",
            "energy": "3.2",
            "cost": "1.76",
        },
        "range_24h": {
            **_range("24h", 48, "4.20", "2.31"),
            "points": [
                {
                    "label": bucket["start"],
                    "energy": bucket["energy"],
                    "cost": bucket["cost"],
                }
                for bucket in recent
            ],
        },
        "range_7d": _range("7d", 7, "17.20", "9.46"),
        "range_30d": _range("30d", 30, "29.20", "16.06"),
        "recent_seven_day_mean_energy": None,
        "recent_seven_day_mean_cost": None,
        "recent_seven_day_change_percent": None,
        "typical_historical_peak_hour": None,
        "estimated_days_remaining": None,
        "anomalies": ["high_vs_baseline"],
        "last_successful_sync": "2026-07-29T10:00:00+08:00",
        "is_stale": True,
        "hourly_profile": hourly,
        "recent_buckets": recent,
    }


class FixtureServer:
    def __init__(
        self,
        *,
        dashboard_status: int = 200,
        charts_available: bool = False,
    ) -> None:
        self.dashboard_status = dashboard_status
        self.charts_available = charts_available
        self.requests: list[str] = []
        self._lock = Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = urlparse(self.path).path
                with outer._lock:
                    outer.requests.append(path)

                if path in {"/", "/dashboard"}:
                    html = TEMPLATE.read_text(encoding="utf-8").replace(
                        ECHARTS_TAG,
                        (
                            """
                            <script>
                            window.__chartOptions = [];
                            window.echarts = {
                              init(element) {
                                return {
                                  setOption(option) {
                                    window.__chartOptions.push({
                                      id: element.id,
                                      option,
                                    });
                                  },
                                  resize() {},
                                };
                              },
                            };
                            </script>
                            """
                            if outer.charts_available
                            else ""
                        ),
                    )
                    self._send(200, html, "text/html; charset=utf-8")
                    return
                if path == "/static/app.js":
                    self._send(
                        200,
                        (STATIC / "app.js").read_text(encoding="utf-8"),
                        "text/javascript; charset=utf-8",
                    )
                    return
                if path == "/static/app.css":
                    self._send(
                        200,
                        (STATIC / "app.css").read_text(encoding="utf-8"),
                        "text/css; charset=utf-8",
                    )
                    return
                if path == "/api/dashboard":
                    if outer.dashboard_status == 401:
                        self._send(
                            401,
                            json.dumps({"detail": "unauthorized"}),
                            "application/json",
                        )
                    else:
                        self._send(
                            200,
                            json.dumps(dashboard_payload()),
                            "application/json",
                        )
                    return
                if path.startswith("/api/day/"):
                    day = path.rsplit("/", 1)[-1]
                    if day == "2026-07-27":
                        time.sleep(0.3)
                    if day == "2026-07-28":
                        time.sleep(0.05)
                    energy = {
                        "2026-07-27": "27",
                        "2026-07-28": "28",
                    }.get(day, "4.2")
                    self._send(
                        200,
                        json.dumps(
                            {
                                "day": day,
                                "total_energy": energy,
                                "total_cost": "2.31",
                                "buckets": [],
                            }
                        ),
                        "application/json",
                    )
                    return
                if path == "/wechat/entry":
                    self._send(
                        200,
                        "<!doctype html><title>OAuth entry</title>",
                        "text/html; charset=utf-8",
                    )
                    return
                self._send(404, "not found", "text/plain")

            def _send(
                self,
                status: int,
                body: str,
                content_type: str,
            ) -> None:
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> FixtureServer:
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class DevTools:
    def __init__(self, websocket_url: str) -> None:
        self._socket = connect(websocket_url)
        self._next_id = 0

    def close(self) -> None:
        self._socket.close()

    def command(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        command_id = self._next_id
        self._socket.send(
            json.dumps(
                {
                    "id": command_id,
                    "method": method,
                    "params": params or {},
                }
            )
        )
        while True:
            message = json.loads(self._socket.recv())
            if message.get("id") == command_id:
                if "error" in message:
                    raise AssertionError(message["error"])
                return message.get("result", {})

    def evaluate(self, expression: str):
        result = self.command(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        remote = result["result"]
        if remote.get("subtype") == "error":
            raise AssertionError(remote.get("description"))
        return remote.get("value")

    def wait_for(self, expression: str, *, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate(expression):
                return
            time.sleep(0.05)
        raise AssertionError(f"browser condition timed out: {expression}")


def _chrome_path() -> str | None:
    candidates = [
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("msedge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    return next(
        (
            str(candidate)
            for candidate in candidates
            if candidate and Path(candidate).is_file()
        ),
        None,
    )


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _json_request(url: str, *, method: str = "GET") -> dict:
    with urlopen(Request(url, method=method), timeout=1) as response:
        return json.loads(response.read())


@contextmanager
def browser_page(url: str):
    chrome = _chrome_path()
    if chrome is None:
        pytest.skip("Chrome or Edge is required for browser automation")
    debugging_port = _free_port()
    with TemporaryDirectory(
        prefix="electricity-browser-",
        ignore_cleanup_errors=True,
    ) as profile:
        process = subprocess.Popen(
            [
                chrome,
                "--headless=new",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-sync",
                "--no-first-run",
                "--no-default-browser-check",
                f"--remote-debugging-port={debugging_port}",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            version_url = (
                f"http://127.0.0.1:{debugging_port}/json/version"
            )
            while True:
                try:
                    _json_request(version_url)
                    break
                except (URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise AssertionError("Chrome DevTools did not start")
                    time.sleep(0.05)
            target = _json_request(
                f"http://127.0.0.1:{debugging_port}/json/new?"
                f"{quote(url, safe='')}",
                method="PUT",
            )
            devtools = DevTools(target["webSocketDebuggerUrl"])
            try:
                devtools.command("Runtime.enable")
                devtools.command("Page.enable")
                yield devtools
            finally:
                try:
                    devtools.command("Browser.close")
                except (ConnectionError, OSError):
                    pass
                devtools.close()
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _text(devtools: DevTools, selector: str) -> str | None:
    return devtools.evaluate(
        f"document.querySelector({json.dumps(selector)})?.textContent"
    )


def test_chart_failure_keeps_text_ranges_and_latest_day_visible_without_fanout():
    with FixtureServer() as server, browser_page(
        f"{server.origin}/dashboard"
    ) as browser:
        browser.wait_for(
            'document.querySelector("#dashboard")?.getAttribute("aria-busy")'
            ' === "false"'
        )

        assert _text(browser, "#today-energy-value") == "4.20 kWh"
        assert _text(browser, "#estimated-days-value") == "数据不足"
        assert _text(browser, "#recent-mean-comparison") == "数据不足"
        assert _text(browser, "#stale-banner") == "数据超过 90 分钟未更新"
        assert _text(browser, "#chart-error") == "图表组件加载失败，文字数据仍可查看。"
        assert _text(browser, "#anomaly-list") == "今日用电明显高于近 7 天同期"
        assert _text(browser, "#typical-peak-text") == "数据不足"

        browser.evaluate(
            'document.querySelector(\'[data-range="7d"]\').click()'
        )
        assert _text(browser, "#range-energy-total") == "17.20 kWh"
        assert _text(browser, "#range-cost-total") == "¥ 9.46"
        assert "2026-07-29" in _text(browser, "#highest-day-text")

        browser.evaluate(
            'document.querySelector(\'[data-range="30d"]\').click()'
        )
        assert _text(browser, "#range-energy-total") == "29.20 kWh"
        assert _text(browser, "#range-cost-total") == "¥ 16.06"

        browser.evaluate(
            """
            const input = document.querySelector("#detail-date");
            input.value = "2026-07-27";
            input.dispatchEvent(new Event("change"));
            input.value = "2026-07-28";
            input.dispatchEvent(new Event("change"));
            """
        )
        browser.wait_for(
            'document.querySelector("#detail-summary")?.textContent'
            '.includes("28.00 kWh")'
        )
        time.sleep(0.35)
        assert "28.00 kWh" in _text(browser, "#detail-summary")

        dashboard_requests = [
            path for path in server.requests if path == "/api/dashboard"
        ]
        day_requests = [
            path for path in server.requests if path.startswith("/api/day/")
        ]
        assert len(dashboard_requests) == 1
        assert len(day_requests) == 3


def test_dashboard_401_redirects_to_wechat_entry_when_charts_are_unavailable():
    with FixtureServer(dashboard_status=401) as server, browser_page(
        f"{server.origin}/dashboard"
    ) as browser:
        browser.wait_for(
            'window.location.pathname === "/wechat/entry"'
        )

        assert browser.evaluate("window.location.pathname") == "/wechat/entry"


def test_range_chart_renders_energy_and_cost_trends():
    with FixtureServer(charts_available=True) as server, browser_page(
        f"{server.origin}/dashboard"
    ) as browser:
        browser.wait_for(
            'document.querySelector("#dashboard")?.getAttribute("aria-busy")'
            ' === "false"'
        )

        browser.evaluate(
            'document.querySelector(\'[data-range="7d"]\').click()'
        )
        trend_option = browser.evaluate(
            """
            window.__chartOptions
              .filter((entry) => entry.id === "trend-chart")
              .at(-1).option
            """
        )

        assert [series["name"] for series in trend_option["series"]] == [
            "用电量",
            "费用",
        ]
        assert trend_option["series"][0]["data"] == [
            1,
            2,
            3,
            4,
            5,
            6,
            7,
        ]
        assert trend_option["series"][1]["data"] == [
            1,
            2,
            3,
            4,
            5,
            6,
            7,
        ]


if __name__ == "__main__":
    with FixtureServer() as preview:
        print(f"{preview.origin}/dashboard", flush=True)
        Event().wait()
