# 805 Electricity Analysis Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a server-only, read-only electricity collection and analysis service for room 7/8F/805, protected by WeChat Official Account OAuth and exposed as a mobile H5 dashboard.

**Architecture:** A FastAPI process owns the property API client, SQLite repository, analytics service, OAuth flow, H5 routes, and an APScheduler job that synchronizes data every 30 minutes. Nginx terminates HTTPS and proxies only to a loopback Uvicorn listener; systemd keeps the service running. Property and WeChat secrets stay in a root-readable environment file and never enter Git or application logs.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, HTTPX, Pydantic Settings, APScheduler, Jinja2, itsdangerous, SQLite, pytest, respx, Nginx, systemd, Apache ECharts 5.4.3.

## Global Constraints

- Support exactly one room: 麒麟科创园 7 号楼 8F 805.
- Poll the authorized property API every 30 minutes and backfill the previous day on every run.
- Reconcile the most recent 30 days once daily.
- Never implement recharge, payment, refund, account modification, or any other property-data write.
- Preserve raw timestamps and values before aggregation.
- Use SQLite with WAL mode and one transaction per synchronization run.
- Require WeChat `snsapi_base` OAuth and an enabled `openid` HMAC allowlist row for every H5 data request.
- Bind the application to `127.0.0.1`; expose only Nginx ports 80 and 443.
- Verify upstream TLS certificates; do not use `verify=False`.
- Keep property credentials, WeChat AppID/AppSecret, session secret, and HMAC key outside Git.
- Redact passwords, tokens, cookies, OAuth codes, `openid`, and upstream response bodies from logs.
- Show a stale-data warning when the last successful synchronization is older than 90 minutes.
- Target Ubuntu 24.04.2 LTS, x86_64, 2 CPU cores, 1.6 GiB RAM, and 40 GB disk.

## Planned File Structure

```text
.
├── .env.example                         # Names of required runtime settings, with empty secret values
├── .gitignore                           # Local environments, databases, logs, tokens, and secrets
├── pyproject.toml                       # Runtime and test dependencies
├── README.md                            # Local setup, credential provisioning, OAuth, and deployment
├── src/electricity_app/
│   ├── __init__.py
│   ├── analytics.py                     # Dashboard calculations over repository data
│   ├── cli.py                           # Local-only database initialization and allowlist commands
│   ├── config.py                        # Validated environment settings
│   ├── db.py                            # SQLite schema, transactions, and repositories
│   ├── domain.py                        # Shared immutable domain types
│   ├── main.py                          # FastAPI application composition and lifespan
│   ├── property_client.py               # Authorized property login, token, pagination, and parsing
│   ├── scheduler.py                     # 30-minute and daily reconciliation jobs
│   ├── sync_service.py                  # Idempotent ingestion orchestration
│   ├── web.py                           # OAuth, session authorization, page, API, and health routes
│   ├── static/
│   │   ├── app.css
│   │   └── app.js
│   └── templates/
│       ├── dashboard.html
│       ├── error.html
│       └── unauthorized.html
├── tests/
│   ├── conftest.py
│   ├── fixtures/property_details.json
│   ├── test_analytics.py
│   ├── test_config.py
│   ├── test_db.py
│   ├── test_property_client.py
│   ├── test_scheduler.py
│   ├── test_sync_service.py
│   └── test_web.py
└── deploy/
    ├── electricity-app.service
    ├── electricity-app.tmpfiles.conf
    ├── nginx-electricity.conf.template
    └── smoke-test.sh
```

---

### Task 1: Project Foundation and Validated Configuration

**Files:**
- Create: `.gitignore`
- Create: `.env.example`
- Create: `pyproject.toml`
- Create: `src/electricity_app/__init__.py`
- Create: `src/electricity_app/config.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Consumes: Environment variables only.
- Produces: `Settings`, `get_settings()`, and the installable `electricity_app` package used by every later task.

- [ ] **Step 1: Write the failing configuration tests**

```python
# tests/test_config.py
import pytest
from pydantic import ValidationError

from electricity_app.config import Settings


def valid_settings() -> dict[str, str]:
    return {
        "property_base_url": "https://zf.zhongkeqizhi.cn:9000",
        "property_username": "authorized-user",
        "property_password": "authorized-password",
        "database_path": "/tmp/electricity-test.db",
        "session_secret": "s" * 32,
        "openid_hmac_key": "h" * 32,
        "wechat_app_id": "wx1234567890abcdef",
        "wechat_app_secret": "w" * 32,
        "public_base_url": "https://electricity.example.test",
    }


def test_settings_accept_https_urls_and_strong_secrets():
    settings = Settings(**valid_settings())
    assert str(settings.property_base_url).startswith("https://")
    assert settings.poll_minutes == 30
    assert settings.timezone == "Asia/Shanghai"


def test_settings_reject_http_property_endpoint():
    values = valid_settings()
    values["property_base_url"] = "http://property.example.test"
    with pytest.raises(ValidationError):
        Settings(**values)


def test_settings_reject_short_session_secret():
    values = valid_settings()
    values["session_secret"] = "short"
    with pytest.raises(ValidationError):
        Settings(**values)
```

- [ ] **Step 2: Run the tests and verify the missing-package failure**

Run:

```bash
python -m pytest tests/test_config.py -v
```

Expected: collection fails because `electricity_app.config` does not exist.

- [ ] **Step 3: Add the package metadata and exact dependencies**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "electricity-analysis"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "apscheduler==3.10.4",
  "fastapi==0.116.1",
  "httpx==0.28.1",
  "itsdangerous==2.2.0",
  "jinja2==3.1.6",
  "pydantic-settings==2.10.1",
  "python-multipart==0.0.20",
  "uvicorn[standard]==0.35.0",
]

[project.optional-dependencies]
test = [
  "pytest==8.4.1",
  "pytest-asyncio==1.1.0",
  "respx==0.22.0",
]

[project.scripts]
electricity-admin = "electricity_app.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
addopts = "-q"
testpaths = ["tests"]
```

Create `.gitignore` with `.venv/`, `*.db`, `*.db-*`, `.env`, `*.log`, `token_cache.txt`, `__pycache__/`, and `.pytest_cache/`. Create `.env.example` with all setting names and empty secret values.

- [ ] **Step 4: Implement strict settings validation**

```python
# src/electricity_app/config.py
from functools import lru_cache
from pathlib import Path

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    property_base_url: AnyHttpUrl
    property_username: str = Field(min_length=1)
    property_password: SecretStr
    database_path: Path
    session_secret: SecretStr = Field(min_length=32)
    openid_hmac_key: SecretStr = Field(min_length=32)
    wechat_app_id: str = Field(pattern=r"^wx[0-9A-Za-z]{16}$")
    wechat_app_secret: SecretStr = Field(min_length=16)
    public_base_url: AnyHttpUrl
    poll_minutes: int = Field(default=30, ge=30, le=30)
    timezone: str = "Asia/Shanghai"
    stale_after_minutes: int = Field(default=90, ge=90, le=90)

    @field_validator("property_base_url", "public_base_url")
    @classmethod
    def require_https(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https":
            raise ValueError("HTTPS is required")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 5: Install and run the configuration tests**

Run:

```bash
python -m pip install -e ".[test]"
python -m pytest tests/test_config.py -v
```

Expected: all three tests pass.

- [ ] **Step 6: Commit the project foundation**

```bash
git add .gitignore .env.example pyproject.toml src/electricity_app/__init__.py src/electricity_app/config.py tests/test_config.py
git commit -m "build: add validated service configuration"
```

---

### Task 2: Domain Types and SQLite Repository

**Files:**
- Create: `src/electricity_app/domain.py`
- Create: `src/electricity_app/db.py`
- Create: `src/electricity_app/cli.py`
- Create: `tests/test_db.py`

**Interfaces:**
- Consumes: `Settings.database_path`.
- Produces: `ElectricityRecord`, `SyncOutcome`, `Database.initialize()`, `Database.upsert_records()`, `Database.apply_sync()`, `Database.list_records()`, `Database.count_records()`, `Database.latest_balance()`, `Database.last_successful_sync()`, `Database.replace_daily_summary()`, `Database.list_daily_summaries()`, `Database.upsert_pending_openid()`, `Database.count_pending_openids()`, and `Database.set_openid_enabled()`.

- [ ] **Step 1: Define repository behavior with failing tests**

```python
# tests/test_db.py
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from electricity_app.db import Database
from electricity_app.domain import ElectricityRecord

TZ = ZoneInfo("Asia/Shanghai")


def record(balance: str = "182.66") -> ElectricityRecord:
    return ElectricityRecord(
        unique_key="upstream-1",
        upstream_id="1",
        room_name="麒麟科创园-7号楼-805",
        device_name="7号楼/8F/805电表",
        occurred_at=datetime(2026, 7, 29, 10, 7, 7, tzinfo=TZ),
        energy=Decimal("0.1"),
        money=Decimal("0.06"),
        rate=Decimal("0.55"),
        balance=Decimal(balance),
    )


def test_upsert_is_idempotent(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    assert db.upsert_records([record()]) == (1, 0)
    assert db.upsert_records([record("182.60")]) == (0, 1)
    rows = db.list_records(date(2026, 7, 29), date(2026, 7, 29))
    assert len(rows) == 1
    assert rows[0].balance == Decimal("182.60")


def test_allowlist_starts_disabled_and_can_be_enabled(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    request_id = db.upsert_pending_openid("digest-value")
    assert db.is_openid_allowed("digest-value") is False
    db.set_openid_enabled(request_id, True)
    assert db.is_openid_allowed("digest-value") is True
```

- [ ] **Step 2: Run the repository tests and verify failure**

Run:

```bash
python -m pytest tests/test_db.py -v
```

Expected: collection fails because `electricity_app.db` and `electricity_app.domain` do not exist.

- [ ] **Step 3: Add immutable domain types**

```python
# src/electricity_app/domain.py
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ElectricityRecord:
    unique_key: str
    upstream_id: str | None
    room_name: str
    device_name: str
    occurred_at: datetime
    energy: Decimal
    money: Decimal
    rate: Decimal | None
    balance: Decimal | None


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    started_at: datetime
    finished_at: datetime
    start_date: date
    end_date: date
    status: str
    fetched: int
    inserted: int
    updated: int
    error_code: str | None = None
```

- [ ] **Step 4: Implement the SQLite schema and repository**

Implement `Database` with a new connection per method, `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, ISO-8601 timestamps, and `Decimal` values stored as text. Create these tables exactly:

```sql
CREATE TABLE IF NOT EXISTS electricity_records (
    unique_key TEXT PRIMARY KEY,
    upstream_id TEXT,
    room_name TEXT NOT NULL,
    device_name TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    energy TEXT NOT NULL,
    money TEXT NOT NULL,
    rate TEXT,
    balance TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_occurred_at
ON electricity_records(occurred_at);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success', 'failed', 'auth_required')),
    fetched INTEGER NOT NULL,
    inserted INTEGER NOT NULL,
    updated INTEGER NOT NULL,
    error_code TEXT
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    observed_at TEXT PRIMARY KEY,
    balance TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('property_detail', 'property_balance'))
);

CREATE TABLE IF NOT EXISTS daily_summaries (
    day TEXT PRIMARY KEY,
    total_energy TEXT NOT NULL,
    total_cost TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    peak_energy TEXT,
    peak_started_at TEXT,
    baseline_energy TEXT,
    anomaly_score TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wechat_allowlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    openid_hmac TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
    created_at TEXT NOT NULL
);
```

Use `INSERT ... ON CONFLICT(unique_key) DO UPDATE` and compare the existing row before writing so `upsert_records()` returns exact inserted and updated counts.

`apply_sync(records, outcome, observed_at)` must execute record upserts, one latest-balance snapshot when a balance is present, and the `sync_runs` insert in a single transaction. `replace_daily_summary()` must upsert one fully calculated day without deleting unrelated dates.

- [ ] **Step 5: Add a local-only allowlist CLI**

Implement:

```python
# src/electricity_app/cli.py
def main() -> None:
    """Commands: init-db, list-pending, enable-wechat REQUEST_ID."""
```

`list-pending` prints only numeric request IDs and creation times. `enable-wechat` changes one disabled row to enabled. It never prints the HMAC digest or raw `openid`.

- [ ] **Step 6: Run repository tests**

Run:

```bash
python -m pytest tests/test_db.py -v
```

Expected: all repository tests pass.

- [ ] **Step 7: Commit the persistence layer**

```bash
git add src/electricity_app/domain.py src/electricity_app/db.py src/electricity_app/cli.py tests/test_db.py
git commit -m "feat: add idempotent electricity repository"
```

---

### Task 3: Authorized Property API Client

**Files:**
- Create: `src/electricity_app/property_client.py`
- Create: `tests/fixtures/property_details.json`
- Create: `tests/test_property_client.py`

**Interfaces:**
- Consumes: `Settings.property_base_url`, username, password, and `ElectricityRecord`.
- Produces: `PropertyClient.fetch_day(day: date) -> list[ElectricityRecord]`, `PropertyAuthenticationError`, `PropertyProtocolError`, and `PropertyUnavailableError`.

- [ ] **Step 1: Add a sanitized response fixture**

```json
{
  "success": true,
  "result": {
    "total": 2,
    "records": [
      {
        "id": "detail-2",
        "roomName": "麒麟科创园-7号楼-805",
        "deviceName": "7号楼/8F/805电表",
        "time": "2026-07-29 10:07:07",
        "energy": "0.1",
        "money": "0.06",
        "rate": "0.55",
        "balance": "182.66"
      },
      {
        "id": "detail-1",
        "roomName": "麒麟科创园-7号楼-805",
        "deviceName": "7号楼/8F/805电表",
        "time": "2026-07-29 09:09:05",
        "energy": "0.1",
        "money": "0.06",
        "rate": "0.55",
        "balance": "182.71"
      }
    ]
  }
}
```

- [ ] **Step 2: Write failing login, pagination, and retry tests**

```python
# tests/test_property_client.py
from datetime import date

import httpx
import pytest
import respx

from electricity_app.property_client import PropertyClient, PropertyProtocolError


@respx.mock
def test_fetch_day_logs_in_and_parses_records(client, fixture_json):
    respx.post("https://zf.zhongkeqizhi.cn:9000/xboot/auth/login").mock(
        return_value=httpx.Response(200, json={"success": True, "result": "token-value"})
    )
    respx.post(
        "https://zf.zhongkeqizhi.cn:9000/xboot/goodits/room/pageBalanceDetails"
    ).mock(return_value=httpx.Response(200, json=fixture_json))
    records = client.fetch_day(date(2026, 7, 29))
    assert len(records) == 2
    assert str(records[0].energy) == "0.1"


@respx.mock
def test_fetch_day_reauthenticates_once_after_auth_failure(client, fixture_json):
    login = respx.post("https://zf.zhongkeqizhi.cn:9000/xboot/auth/login").mock(
        side_effect=[
            httpx.Response(200, json={"success": True, "result": "token-1"}),
            httpx.Response(200, json={"success": True, "result": "token-2"}),
        ]
    )
    details = respx.post(
        "https://zf.zhongkeqizhi.cn:9000/xboot/goodits/room/pageBalanceDetails"
    ).mock(
        side_effect=[
            httpx.Response(401, json={"success": False}),
            httpx.Response(200, json=fixture_json),
        ]
    )
    assert len(client.fetch_day(date(2026, 7, 29))) == 2
    assert login.call_count == 2
    assert details.call_count == 2


@respx.mock
def test_missing_records_is_a_protocol_error(client):
    respx.post("https://zf.zhongkeqizhi.cn:9000/xboot/auth/login").mock(
        return_value=httpx.Response(200, json={"success": True, "result": "token"})
    )
    respx.post(
        "https://zf.zhongkeqizhi.cn:9000/xboot/goodits/room/pageBalanceDetails"
    ).mock(return_value=httpx.Response(200, json={"success": True, "result": {}}))
    with pytest.raises(PropertyProtocolError):
        client.fetch_day(date(2026, 7, 29))
```

Add fixtures in `tests/conftest.py` that construct `Settings`, load the JSON fixture, and create `PropertyClient` with an injected `httpx.Client`.

- [ ] **Step 3: Run the client tests and verify failure**

Run:

```bash
python -m pytest tests/test_property_client.py -v
```

Expected: collection fails because `electricity_app.property_client` does not exist.

- [ ] **Step 4: Implement secure login and paginated reads**

Implement:

```python
class PropertyClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None: ...
    def login(self) -> None: ...
    def fetch_day(self, day: date) -> list[ElectricityRecord]: ...
    def _fetch_page(self, day: date, page_number: int) -> dict[str, object]: ...
    def _parse_record(self, item: dict[str, object]) -> ElectricityRecord: ...
```

Required behavior:

- Create `httpx.Client(base_url=..., verify=True, timeout=httpx.Timeout(10, read=30))`.
- Send form-encoded username/password only to `/xboot/auth/login`.
- Keep the token only in process memory.
- Send `Accesstoken` only to the configured property origin.
- Request `/xboot/goodits/room/pageBalanceDetails` with `pageNumber`, `pageSize=100`, `type=2`, `startDate`, and `endDate`.
- Follow pages until fetched count reaches `result.total` or a page returns fewer than 100 records.
- Stop after 100 pages and raise `PropertyProtocolError`.
- On HTTP 401, 403, or an application-level authentication failure, clear the token, log in, and retry exactly once.
- Never log form data, token headers, or full response bodies.
- Generate `unique_key` from upstream ID; if absent, SHA-256 the room, device, timestamp, energy, money, and rate.
- Parse timestamps in `Asia/Shanghai`.

- [ ] **Step 5: Run property client tests**

Run:

```bash
python -m pytest tests/test_property_client.py -v
```

Expected: all property client tests pass.

- [ ] **Step 6: Run an authorized schema probe without exposing values**

With credentials configured only in the local `.env`, add and run:

```bash
python -m electricity_app.cli probe-property-schema
```

The command performs one authorized current-day fetch and prints only sorted field names, record count, and whether each required field is present. It must not print field values, credentials, Token, or the response body. Update `_parse_record()` and the sanitized fixture if actual field names differ, then rerun the tests.

- [ ] **Step 7: Commit the property connector**

```bash
git add src/electricity_app/property_client.py src/electricity_app/cli.py tests/conftest.py tests/fixtures/property_details.json tests/test_property_client.py
git commit -m "feat: add secure property electricity client"
```

---

### Task 4: Idempotent Synchronization Service

**Files:**
- Create: `src/electricity_app/sync_service.py`
- Create: `tests/test_sync_service.py`

**Interfaces:**
- Consumes: `PropertyClient.fetch_day()` and `Database` repository methods.
- Produces: `SyncService.sync_dates(start_date, end_date) -> SyncOutcome`, `SyncService.sync_recent(now) -> SyncOutcome`, `SyncService.reconcile_30_days(now) -> SyncOutcome`, `SyncService.sync_recent_now() -> SyncOutcome`, and `SyncService.reconcile_30_days_now() -> SyncOutcome`.

- [ ] **Step 1: Write failing synchronization tests**

```python
# tests/test_sync_service.py
from datetime import date, datetime
from zoneinfo import ZoneInfo

from electricity_app.sync_service import SyncService

TZ = ZoneInfo("Asia/Shanghai")


def test_recent_sync_fetches_today_and_yesterday(fake_client, db):
    now = datetime(2026, 7, 29, 10, 30, tzinfo=TZ)
    outcome = SyncService(fake_client, db).sync_recent(now)
    assert fake_client.requested_days == [
        now.date().replace(day=28),
        now.date(),
    ]
    assert outcome.status == "success"


def test_failed_sync_keeps_existing_records(failing_client, populated_db):
    before = populated_db.count_records()
    outcome = SyncService(failing_client, populated_db).sync_recent(
        datetime(2026, 7, 29, 10, 30, tzinfo=TZ)
    )
    assert outcome.status == "failed"
    assert populated_db.count_records() == before
```

- [ ] **Step 2: Run the synchronization tests and verify failure**

Run:

```bash
python -m pytest tests/test_sync_service.py -v
```

Expected: collection fails because `electricity_app.sync_service` does not exist.

- [ ] **Step 3: Implement atomic synchronization**

```python
class SyncService:
    def __init__(self, client: PropertyClient, db: Database) -> None: ...
    def sync_dates(self, start_date: date, end_date: date) -> SyncOutcome: ...
    def sync_recent(self, now: datetime) -> SyncOutcome: ...
    def reconcile_30_days(self, now: datetime) -> SyncOutcome: ...
    def sync_recent_now(self) -> SyncOutcome: ...
    def reconcile_30_days_now(self) -> SyncOutcome: ...
```

The two `*_now` methods obtain the current `Asia/Shanghai` time and delegate to their deterministic counterparts.

Fetch every date before opening the write transaction. If any date fails, write one failed `sync_runs` row and preserve all existing electricity records. If all fetches succeed, call `Database.apply_sync()` so record upserts, the latest available balance snapshot, and the successful `sync_runs` row share one transaction. Map authentication exhaustion to `auth_required`; map transport and protocol failures to `failed`.

- [ ] **Step 4: Run synchronization tests**

Run:

```bash
python -m pytest tests/test_sync_service.py -v
```

Expected: all synchronization tests pass.

- [ ] **Step 5: Commit the synchronization service**

```bash
git add src/electricity_app/sync_service.py tests/test_sync_service.py
git commit -m "feat: synchronize authorized electricity records"
```

---

### Task 5: Electricity Analytics

**Files:**
- Create: `src/electricity_app/analytics.py`
- Create: `tests/test_analytics.py`

**Interfaces:**
- Consumes: `Database.list_records()`, `Database.latest_balance()`, and `Database.last_successful_sync()`.
- Produces: `AnalyticsService.dashboard(now) -> DashboardSummary`, `AnalyticsService.day_detail(day) -> DayDetail`, and `AnalyticsService.rebuild_daily_summaries(start_date, end_date, now) -> int`.

- [ ] **Step 1: Write failing metric tests with fixed records**

```python
# tests/test_analytics.py
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from electricity_app.analytics import AnalyticsService

TZ = ZoneInfo("Asia/Shanghai")


def test_dashboard_calculates_totals_and_day_comparison(analytics_db):
    summary = AnalyticsService(analytics_db).dashboard(
        datetime(2026, 7, 29, 12, 0, tzinfo=TZ)
    )
    assert summary.today_energy == Decimal("4.2")
    assert summary.today_cost == Decimal("2.31")
    assert summary.yesterday_energy == Decimal("3.0")
    assert summary.day_change_percent == Decimal("40.0")


def test_balance_days_is_none_when_history_is_insufficient(short_history_db):
    summary = AnalyticsService(short_history_db).dashboard(
        datetime(2026, 7, 29, 12, 0, tzinfo=TZ)
    )
    assert summary.estimated_days_remaining is None


def test_dashboard_is_stale_after_90_minutes(analytics_db):
    summary = AnalyticsService(analytics_db).dashboard(
        datetime(2026, 7, 29, 14, 1, tzinfo=TZ)
    )
    assert summary.is_stale is True


def test_rebuild_daily_summaries_persists_one_row_per_day(analytics_db):
    service = AnalyticsService(analytics_db)
    written = service.rebuild_daily_summaries(
        date(2026, 7, 28),
        date(2026, 7, 29),
        datetime(2026, 7, 29, 14, 1, tzinfo=TZ),
    )
    assert written == 2
    assert len(analytics_db.list_daily_summaries(date(2026, 7, 28), date(2026, 7, 29))) == 2
```

- [ ] **Step 2: Run analytics tests and verify failure**

Run:

```bash
python -m pytest tests/test_analytics.py -v
```

Expected: collection fails because `electricity_app.analytics` does not exist.

- [ ] **Step 3: Define exact analytics result types**

Add to `domain.py`:

```python
@dataclass(frozen=True, slots=True)
class TimeBucket:
    start: datetime
    energy: Decimal
    cost: Decimal


@dataclass(frozen=True, slots=True)
class DashboardSummary:
    balance: Decimal | None
    today_energy: Decimal
    today_cost: Decimal
    yesterday_energy: Decimal
    day_change_percent: Decimal | None
    seven_day_energy: Decimal
    thirty_day_energy: Decimal
    daily_average_energy: Decimal | None
    peak_bucket: TimeBucket | None
    estimated_days_remaining: Decimal | None
    anomalies: tuple[str, ...]
    last_successful_sync: datetime | None
    is_stale: bool
    hourly_profile: tuple[TimeBucket, ...]
    recent_buckets: tuple[TimeBucket, ...]


@dataclass(frozen=True, slots=True)
class DayDetail:
    day: date
    total_energy: Decimal
    total_cost: Decimal
    buckets: tuple[TimeBucket, ...]
```

- [ ] **Step 4: Implement deterministic aggregation**

Implement `AnalyticsService` with these rules:

- Half-hour buckets use `[start, start + 30 minutes)` in `Asia/Shanghai`.
- Sum all records in a bucket; never overwrite one record with another.
- Day totals use local calendar boundaries.
- Day comparison is `(today - yesterday) / yesterday * 100`, rounded to one decimal; return `None` when yesterday is zero.
- Estimated days use the latest positive balance divided by mean daily cost over the latest seven complete days; require at least three complete days and positive mean cost.
- Flag `high_vs_baseline` when today's energy after the same elapsed time is at least 50% above the mean of the previous seven comparable days and at least 1 kWh higher.
- Flag `continuous_night_load` when every half-hour bucket from 00:00 through 05:00 is non-zero and their mean is above the previous seven-day night mean by 50%.
- Mark stale when no successful sync exists or it is older than the configured 90-minute threshold.
- `rebuild_daily_summaries()` calculates each requested local day and upserts exactly one `daily_summaries` row through `Database.replace_daily_summary()`.

- [ ] **Step 5: Run analytics tests**

Run:

```bash
python -m pytest tests/test_analytics.py -v
```

Expected: all analytics tests pass.

- [ ] **Step 6: Commit analytics**

```bash
git add src/electricity_app/domain.py src/electricity_app/analytics.py tests/test_analytics.py
git commit -m "feat: calculate electricity trends and anomalies"
```

---

### Task 6: WeChat OAuth, HMAC Allowlist, and Sessions

**Files:**
- Create: `src/electricity_app/web.py`
- Create: `tests/test_web.py`

**Interfaces:**
- Consumes: `Settings`, `Database`, `AnalyticsService`, HTTPX, and FastAPI sessions.
- Produces: `create_router(settings, db, analytics, wechat_http) -> APIRouter`, `/wechat/entry`, `/wechat/callback`, `/api/dashboard`, `/api/day/{day}`, `/health/live`, and `/health/ready`.

- [ ] **Step 1: Write failing OAuth and authorization tests**

```python
# tests/test_web.py
def test_wechat_entry_redirects_with_signed_state(client):
    response = client.get("/wechat/entry", follow_redirects=False)
    assert response.status_code == 307
    assert "open.weixin.qq.com/connect/oauth2/authorize" in response.headers["location"]
    assert "scope=snsapi_base" in response.headers["location"]


def test_callback_creates_pending_request_for_unknown_openid(
    client, wechat_oauth_mock, db
):
    response = client.get("/wechat/callback?code=valid-code&state=valid-state")
    assert response.status_code == 403
    assert db.count_pending_openids() == 1
    assert "openid-value" not in response.text


def test_dashboard_api_rejects_session_without_enabled_allowlist(client):
    response = client.get("/api/dashboard")
    assert response.status_code == 401


def test_dashboard_api_allows_enabled_openid(authorized_client):
    response = authorized_client.get("/api/dashboard")
    assert response.status_code == 200
    assert "today_energy" in response.json()
```

- [ ] **Step 2: Run OAuth tests and verify failure**

Run:

```bash
python -m pytest tests/test_web.py -v
```

Expected: collection fails because `electricity_app.web` does not exist.

- [ ] **Step 3: Implement OAuth state and HMAC identity handling**

Use `itsdangerous.URLSafeTimedSerializer` with the session secret. OAuth state contains a random nonce and issuance time and is accepted for at most 300 seconds.

Compute identity as:

```python
def openid_hmac(openid: str, key: bytes) -> str:
    return hmac.new(key, openid.encode("utf-8"), hashlib.sha256).hexdigest()
```

Exchange the code only at `https://api.weixin.qq.com/sns/oauth2/access_token` with configured AppID and AppSecret. Do not log the URL query, response body, OAuth code, access token, or raw `openid`.

Unknown identities are inserted into `wechat_allowlist` with `enabled=0`; the response shows only the numeric request ID. Enabled identities receive a signed session containing the HMAC digest, not raw `openid`.

- [ ] **Step 4: Add route authorization and health semantics**

`/api/dashboard` and `/api/day/{day}` require a valid session and an enabled database row. `/health/live` returns 200 whenever the process runs. `/health/ready` returns:

- 200 with `{"status": "ready"}` after database initialization.
- 503 with `{"status": "auth_required"}` after an exhausted property authentication failure.
- 200 with `{"status": "degraded"}` for stale data while historical reads remain available.

- [ ] **Step 5: Run web authorization tests**

Run:

```bash
python -m pytest tests/test_web.py -v
```

Expected: all OAuth, allowlist, session, and health tests pass.

- [ ] **Step 6: Commit WeChat access control**

```bash
git add src/electricity_app/web.py tests/test_web.py
git commit -m "feat: protect dashboard with WeChat OAuth"
```

---

### Task 7: Mobile H5 Dashboard

**Files:**
- Create: `src/electricity_app/templates/dashboard.html`
- Create: `src/electricity_app/templates/error.html`
- Create: `src/electricity_app/templates/unauthorized.html`
- Create: `src/electricity_app/static/app.css`
- Create: `src/electricity_app/static/app.js`
- Modify: `src/electricity_app/web.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- Consumes: protected JSON routes from Task 6.
- Produces: `/dashboard` mobile page with summary cards, range controls, trend chart, comparison, hourly profile, anomalies, day detail, and stale-state banner.

- [ ] **Step 1: Add failing page-content tests**

```python
def test_dashboard_page_contains_required_sections(authorized_client):
    response = authorized_client.get("/dashboard")
    assert response.status_code == 200
    assert 'id="balance-card"' in response.text
    assert 'id="trend-chart"' in response.text
    assert 'id="hourly-chart"' in response.text
    assert 'id="anomaly-list"' in response.text
    assert 'id="stale-banner"' in response.text


def test_dashboard_page_redirects_unauthenticated_user(client):
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/wechat/entry"
```

- [ ] **Step 2: Run H5 tests and verify failure**

Run:

```bash
python -m pytest tests/test_web.py -k dashboard_page -v
```

Expected: tests fail because `/dashboard` and its template do not exist.

- [ ] **Step 3: Build the semantic mobile layout**

Create accessible HTML with:

- Four top cards: balance, today energy, today cost, estimated days.
- Range buttons with `data-range="24h"`, `7d`, and `30d`.
- ECharts containers for trend and hourly distribution.
- Comparison text, anomaly list, date input, detail table, last-sync text, and stale banner.
- An external pinned script URL `https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js`; no inline JavaScript.

Use Chinese UI copy encoded as UTF-8. Add `lang="zh-CN"`, a viewport meta tag, loading states, empty states, and screen-reader labels.

- [ ] **Step 4: Implement safe front-end rendering**

In `app.js`:

- Fetch only same-origin `/api/dashboard` and `/api/day/{date}`.
- Use `textContent` for textual values; never insert API values with `innerHTML`.
- Render 24-hour, 7-day, and 30-day series from API data.
- Display `数据超过 90 分钟未更新` when `is_stale` is true.
- Display `数据不足` when an estimate or comparison is null.
- Resize both charts on `window.resize`.
- Redirect to `/wechat/entry` on HTTP 401.

- [ ] **Step 5: Add responsive CSS**

Use a 420 px mobile-first canvas, CSS grid cards, minimum 44 px touch targets, high-contrast stale and anomaly states, and `prefers-reduced-motion`. At 768 px and above, allow a maximum page width of 960 px without changing information order.

- [ ] **Step 6: Run H5 route tests**

Run:

```bash
python -m pytest tests/test_web.py -v
```

Expected: all web tests pass.

- [ ] **Step 7: Perform a browser smoke check**

Run the test app with a development-only fixture identity and inspect at 375×812 and 414×896 viewports. Verify no horizontal scrolling, all cards are readable, range controls work, stale state is visible, and Chinese text is not garbled.

- [ ] **Step 8: Commit the H5 dashboard**

```bash
git add src/electricity_app/templates src/electricity_app/static src/electricity_app/web.py tests/test_web.py
git commit -m "feat: add mobile electricity dashboard"
```

---

### Task 8: Scheduler and Application Composition

**Files:**
- Create: `src/electricity_app/scheduler.py`
- Create: `src/electricity_app/main.py`
- Create: `tests/test_scheduler.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: all services from Tasks 1–7.
- Produces: `create_app(settings: Settings | None = None) -> FastAPI`, `create_scheduler(sync_service, analytics_service, timezone) -> BackgroundScheduler`, and `electricity_app.main:app`.

- [ ] **Step 1: Write failing scheduler tests**

```python
# tests/test_scheduler.py
from electricity_app.scheduler import create_scheduler


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
```

- [ ] **Step 2: Run scheduler tests and verify failure**

Run:

```bash
python -m pytest tests/test_scheduler.py -v
```

Expected: collection fails because `electricity_app.scheduler` does not exist.

- [ ] **Step 3: Implement scheduler jobs**

Create an APScheduler `BackgroundScheduler` in `Asia/Shanghai`:

```python
scheduler.add_job(
    sync_service.sync_recent_now,
    "cron",
    minute="0,30",
    id="sync_recent",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=900,
)
scheduler.add_job(
    sync_service.reconcile_30_days_now,
    "cron",
    hour=2,
    minute=15,
    id="reconcile_30_days",
    max_instances=1,
    coalesce=True,
)
```

Use a process-local non-blocking lock around both methods so reconciliation and recent sync cannot overlap.

Wrap each scheduled call so a successful synchronization immediately invokes `AnalyticsService.rebuild_daily_summaries()` for the synchronized date range. The recent job rebuilds yesterday and today; the daily reconciliation job rebuilds the latest 30 local calendar days.

- [ ] **Step 4: Compose the FastAPI lifespan**

`create_app()` must:

1. Load settings.
2. Initialize the database.
3. Construct one property HTTP client, repository, sync service, analytics service, and web router.
4. Mount static files and templates.
5. Add `SessionMiddleware` with `https_only=True`, `same_site="lax"`, and the configured secret.
6. Start the scheduler during lifespan startup.
7. Trigger one non-blocking recent sync after startup.
8. Shut down scheduler and HTTP clients during lifespan shutdown.

Add structured log filtering that replaces values for keys matching `password`, `token`, `cookie`, `authorization`, `openid`, and `code` with `[redacted]`.

- [ ] **Step 5: Run scheduler and full test suite**

Run:

```bash
python -m pytest tests/test_scheduler.py -v
python -m pytest -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit application composition**

```bash
git add src/electricity_app/scheduler.py src/electricity_app/main.py tests/test_scheduler.py tests/conftest.py
git commit -m "feat: schedule electricity synchronization"
```

---

### Task 9: Production Deployment Assets

**Files:**
- Create: `deploy/electricity-app.service`
- Create: `deploy/electricity-app.tmpfiles.conf`
- Create: `deploy/nginx-electricity.conf.template`
- Create: `deploy/smoke-test.sh`
- Create: `README.md`

**Interfaces:**
- Consumes: installable application and runtime environment file.
- Produces: repeatable Ubuntu installation, loopback application service, HTTPS reverse proxy template, backup instructions, and smoke checks.

- [ ] **Step 1: Create the hardened systemd unit**

Use:

```ini
[Unit]
Description=805 Electricity Analysis
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=electricity
Group=electricity
WorkingDirectory=/opt/electricity-app
EnvironmentFile=/etc/electricity-app/electricity.env
ExecStart=/opt/electricity-app/.venv/bin/uvicorn electricity_app.main:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=on-failure
RestartSec=5
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/electricity-app
NoNewPrivileges=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
```

Create a tmpfiles rule that owns `/var/lib/electricity-app` as `electricity:electricity` with mode `0750`.

- [ ] **Step 2: Create the Nginx template**

The template must:

- Redirect port 80 to HTTPS.
- Use `${PUBLIC_HOST}`, `${TLS_CERTIFICATE}`, and `${TLS_CERTIFICATE_KEY}` substitutions.
- Proxy to `http://127.0.0.1:8000`.
- Set `X-Forwarded-Proto`, `X-Forwarded-For`, and `Host`.
- Limit OAuth and API routes.
- Add HSTS only on HTTPS.
- Add `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, and a CSP permitting same-origin assets plus the pinned jsDelivr ECharts script.
- Deny access to dotfiles and paths beginning with `/admin`.

- [ ] **Step 3: Add an exact smoke-test script**

`deploy/smoke-test.sh` must use `set -euo pipefail` and verify:

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/health/live
curl --fail --silent --show-error https://"$PUBLIC_HOST"/health/live
test "$(curl --silent --output /dev/null --write-out '%{http_code}' https://"$PUBLIC_HOST"/api/dashboard)" = "401"
ss -lnt | grep -q '127.0.0.1:8000'
! ss -lnt | grep -q '0.0.0.0:8000'
```

- [ ] **Step 4: Document installation and secret provisioning**

README instructions must cover:

1. Create the `electricity` system user.
2. Install Python 3.12 venv and Nginx.
3. Copy the repository to `/opt/electricity-app`.
4. Install with `pip install .`.
5. Create `/etc/electricity-app/electricity.env` with mode `0600`.
6. Initialize SQLite with `electricity-admin init-db`.
7. Install systemd and tmpfiles definitions.
8. Render the Nginx template with `envsubst`.
9. Validate with `nginx -t`.
10. Enable and start services.
11. Use SSH port forwarding before a domain exists.
12. Configure the formal HTTPS domain, WeChat web authorization domain, and menu.
13. Approve the first OAuth request with `electricity-admin list-pending` and `enable-wechat`.
14. Back up SQLite using `sqlite3 /var/lib/electricity-app/electricity.db ".backup '/var/backups/electricity-app/electricity-$(date +%F).db'"`.
15. Restore into a stopped service and run `PRAGMA integrity_check`.

Do not include real credentials, Token values, `openid`, or the actual future domain.

- [ ] **Step 5: Validate deployment files locally**

Run:

```bash
python -m pytest -v
git diff --check
```

On Ubuntu, run:

```bash
systemd-analyze verify deploy/electricity-app.service
shellcheck deploy/smoke-test.sh
```

Expected: tests pass, no whitespace errors, systemd verification succeeds, and ShellCheck reports no findings.

- [ ] **Step 6: Commit deployment assets**

```bash
git add deploy README.md
git commit -m "ops: add hardened Ubuntu deployment"
```

---

### Task 10: End-to-End Verification and Production Readiness

**Files:**
- Modify: `README.md`
- Modify: tests only if a verified production discrepancy requires a regression test.

**Interfaces:**
- Consumes: the complete application and deployment assets.
- Produces: verified local behavior, verified authorized property integration, and a documented production handoff.

- [ ] **Step 1: Run all automated checks from a clean environment**

Run:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
python -m pytest -v
```

Expected: all tests pass with no external property or WeChat network access.

- [ ] **Step 2: Run an authorized one-day integration check**

With secrets configured locally and no debug logging:

```bash
electricity-admin probe-property-schema
electricity-admin sync-date 2026-07-29
electricity-admin summarize-date 2026-07-29
```

Compare record count, total energy, total cost, latest balance, and at least three half-hour records with the authorized property mini program. If any value differs, add a sanitized regression fixture and failing test before changing parsing or aggregation.

- [ ] **Step 3: Verify access-control boundaries**

Confirm:

- A request without a session gets HTTP 401 from `/api/dashboard`.
- A valid OAuth identity with a disabled allowlist row gets HTTP 403.
- Enabling the numeric request ID permits the dashboard.
- Disabling the row revokes the next request.
- Logs contain no raw `openid`, OAuth code, access token, property password, or property Token.

- [ ] **Step 4: Verify scheduler recovery**

Run the app against a temporary database, allow one sync, restart it, and verify:

- Existing records remain.
- Startup schedules both jobs once.
- A missed run coalesces instead of running concurrently.
- Duplicate upstream records do not increase the database count.
- Stale data produces a visible warning after 90 minutes.

- [ ] **Step 5: Deploy to the Ubuntu server without public data exposure**

Before the formal domain is ready:

- Bind Uvicorn only to `127.0.0.1:8000`.
- Use SSH port forwarding for inspection.
- Do not add an IP-based public H5 endpoint.

After the formal domain, DNS, certificate, and WeChat authorization domain are ready:

- Render and enable the Nginx HTTPS configuration.
- Run `deploy/smoke-test.sh`.
- Confirm only ports 22, 80, and 443 are externally reachable.
- Configure the公众号 menu to the formal `/wechat/entry` URL.

- [ ] **Step 6: Complete the 24-hour acceptance run**

Record these results in README under a dated “Production acceptance” section:

- At least 48 scheduled opportunities occurred over 24 hours.
- Every successful run is idempotent.
- Any failed run preserved history and exposed a degraded health state.
- Daily totals match the property mini program.
- Mobile layouts at 375×812 and 414×896 have no horizontal overflow.
- Service and scheduler recover after one controlled server restart.

- [ ] **Step 7: Commit verified readiness notes**

```bash
git add README.md tests
git commit -m "test: document production acceptance"
```

## Completion Criteria

- All automated tests pass from a clean Python 3.12 environment.
- The authorized property schema probe confirms the parser without printing sensitive values.
- The service synchronizes current and previous-day records every 30 minutes.
- Repeated synchronization produces no duplicate database rows.
- Analytics match property records for a manually verified date.
- WeChat OAuth plus enabled HMAC allowlist is required for all resident data.
- Credentials and identifiers are absent from Git and logs.
- The H5 page is usable inside WeChat on common mobile widths.
- The Ubuntu service survives restart and exposes only Nginx publicly.
- A formal HTTPS domain is used before the公众号 menu is enabled.
