# Electricity Analysis Final Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every final-review finding while restoring the complete approved electricity analytics and preserving the service’s single-room, read-only security boundary.

**Architecture:** Keep the existing FastAPI/SQLite service, but make repository connection ownership explicit, validate and retry at the property-client boundary, and return all dashboard range data in one protected API response. Keep textual H5 rendering independent from the optional ECharts adapter, and add local-only operational commands plus hardened Nginx and systemd assets.

**Tech Stack:** Python 3.12, SQLite, FastAPI, HTTPX, APScheduler, vanilla JavaScript with local headless-Chrome automation, Nginx, and systemd.

## Global Constraints

- The approved design document governs over the narrower original implementation plan.
- Support only 麒麟科创园 7 号楼 8F 805 and reject every mixed-room, wrong-device, or wrong-Shanghai-day upstream response before storage.
- Never use, request, print, or store real property or WeChat credentials.
- Use Decimal for all energy, cost, rate, balance, mean, and percentage calculations.
- Preserve selected-day CLI balance behavior while ordering the current balance by the detail’s effective timestamp.
- Retry only network/TLS failures and HTTP 5xx with bounded exponential backoff; never retry arbitrary 4xx.
- Do not claim live property, WeChat, Ubuntu, DNS, TLS, or 24-hour production acceptance.

---

### Task 1: SQLite lifecycle, balance ordering, and auth-gate state

**Files:**
- Modify: `src/electricity_app/db.py`
- Modify: `tests/test_db.py`

**Interfaces:**
- Produces: `Database.connection()`, `Database.auth_gate_active()`, `Database.clear_auth_gate()`, and `Database.backup_to()`.
- Preserves: all existing repository method signatures and `latest_balance_for_day()`.

- [ ] **Step 1: Write failing connection and balance regressions**

```python
def test_database_closes_connections_after_success_and_failure(tmp_path):
    # Use a sqlite3.Connection subclass that records close(), then exercise
    # initialize/count and a duplicate-nonce IntegrityError.

def test_historical_backfill_cannot_replace_current_balance(tmp_path):
    # Apply a current detail snapshot first and an older detail later.
    # latest_balance() must retain the current detail value.
```

- [ ] **Step 2: Run red tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py -k "closes_connections or historical_backfill" -v`

Expected: connection close assertions fail and the late historical sync replaces the current balance.

- [ ] **Step 3: Implement explicit transaction/close ownership**

Add a context manager that opens/configures a connection, commits on success, rolls back on failure, and closes in `finally`. Route every repository method through it. Add `balance_snapshots.effective_at`, migrate existing databases safely, and order current snapshots by effective time before observation time.

- [ ] **Step 4: Run green tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py -v`

---

### Task 2: Fail-closed property client, bounded retry, and safe diagnostics

**Files:**
- Modify: `src/electricity_app/config.py`
- Modify: `.env.example`
- Modify: `src/electricity_app/property_client.py`
- Modify: `src/electricity_app/sync_service.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_property_client.py`
- Modify: `tests/test_sync_service.py`

**Interfaces:**
- Produces: configured `property_room_name` and `property_device_name`; stable exception `code` values; process-restart/local-reset auth gate behavior.

- [ ] **Step 1: Write failing boundary regressions**

```python
def test_fetch_day_rejects_mixed_room_records_before_returning(): ...
def test_fetch_day_rejects_a_record_from_another_shanghai_day(): ...
def test_non_finite_decimal_values_are_protocol_errors(): ...
def test_network_and_5xx_retry_is_bounded_with_exponential_delays(): ...
def test_details_4xx_is_not_retried(): ...
def test_exact_detail_form_parameters_are_sent(): ...
def test_page_limit_stops_after_exactly_100_pages(): ...
def test_application_auth_failure_reauthenticates_once_then_fails(): ...
def test_auth_gate_prevents_later_property_calls_until_local_reset(): ...
```

- [ ] **Step 2: Run red tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_property_client.py tests/test_sync_service.py tests/test_config.py -v`

- [ ] **Step 3: Implement validation, retries, diagnostics, and gate**

Validate all parsed records against exact configured room/device strings and requested Shanghai day. Reject NaN/Infinity. Use three total attempts with deterministic exponential delays injected for tests. Map failures to `invalid_json`, `pagination_overflow`, `scope_mismatch`, `wrong_day`, `tls_network`, `upstream_5xx`, `authentication`, or `protocol` without response bodies or identifiers. Persist an auth gate after `auth_required`; clear it only from the local reset operation or application composition on restart.

- [ ] **Step 4: Run green tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_property_client.py tests/test_sync_service.py tests/test_config.py -v`

---

### Task 3: Restore approved range analytics and one-response API

**Files:**
- Modify: `src/electricity_app/domain.py`
- Modify: `src/electricity_app/analytics.py`
- Modify: `src/electricity_app/web.py`
- Modify: `tests/test_analytics.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- Produces: explicit `RangeAnalytics`/`AnalyticsPoint` values for 24h, 7d, and 30d energy and cost; recent-seven-day means/comparison; selected-range highest day; historical typical peak hour.

- [ ] **Step 1: Write failing Decimal/calendar analytics tests**

```python
def test_dashboard_exposes_energy_and_cost_for_all_ranges(): ...
def test_dashboard_compares_today_with_recent_seven_day_mean(): ...
def test_ranges_expose_highest_use_day_with_deterministic_ties(): ...
def test_typical_historical_peak_is_distinct_from_today_peak(): ...
def test_insufficient_history_returns_explicit_null_metrics(): ...
def test_dashboard_api_contains_all_range_series_in_one_response(): ...
```

- [ ] **Step 2: Run red tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_analytics.py tests/test_web.py -v`

- [ ] **Step 3: Implement deterministic analytics**

Zero-fill 48 Shanghai half-hours and 7/30 Shanghai calendar-day series, sum Decimal energy and cost, use earliest day/hour for ties, require at least three prior populated days for mean/typical metrics, and expose `None` when data is insufficient.

- [ ] **Step 4: Run green tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_analytics.py tests/test_web.py -v`

---

### Task 4: H5 text-first rendering and JavaScript automation

**Files:**
- Modify: `src/electricity_app/templates/dashboard.html`
- Modify: `src/electricity_app/static/app.js`
- Modify: `src/electricity_app/static/app.css`
- Create: `tests/test_frontend_browser.py`

**Interfaces:**
- Produces: one dashboard fetch, local range switching, chart-specific failure handling, exact Chinese stale/null states, 401 redirect, and last-request-wins day detail.

- [ ] **Step 1: Write failing headless-Chrome automation**

Serve the real template and JavaScript from a local fixture server and drive a local headless Chrome target through the DevTools protocol. Assert only `/api/dashboard` plus the selected detail day are requested, switching 24h/7d/30d performs no daily fan-out, 401 navigates to `/wechat/entry`, `数据不足` and `数据超过 90 分钟未更新` are exact, chart initialization failure leaves summary/comparison/anomaly/detail rendering active, and stale competing detail promises cannot overwrite the latest selection.

- [ ] **Step 2: Run red tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_frontend_browser.py -v`

- [ ] **Step 3: Refactor browser wiring around a testable controller**

Render summary, comparison, anomalies, freshness, selected-range totals/highest day, typical peak, and day detail before or independently of ECharts initialization. Catch chart errors only in the chart adapter.

- [ ] **Step 4: Run green tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_frontend_browser.py -v`

---

### Task 5: Nginx OAuth privacy and explicit session age

**Files:**
- Modify: `deploy/nginx-electricity.conf.template`
- Modify: `src/electricity_app/config.py`
- Modify: `src/electricity_app/main.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_scheduler.py`
- Create: `tests/test_deployment.py`

**Interfaces:**
- Produces: `electricity_privacy` access log based on `$uri`, callback logging suppression, `Referrer-Policy: no-referrer`, and a 30-minute session maximum age.

- [ ] **Step 1: Write failing rendered/static configuration tests**

Render placeholders, assert the log format contains `$uri` but no `$request_uri`, `$args`, or `$http_referer`; assert exact callback access/error suppression; assert `no-referrer`; assert middleware `max_age=1800`.

- [ ] **Step 2: Run red tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_deployment.py tests/test_scheduler.py tests/test_config.py -v`

- [ ] **Step 3: Implement privacy/session configuration**

Use the privacy log globally, drop query arguments from the HTTP redirect, isolate callback logging, and configure a validated fixed 1800-second default session age.

- [ ] **Step 4: Run green tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_deployment.py tests/test_scheduler.py tests/test_config.py -v`

---

### Task 6: Backup and revocation operations

**Files:**
- Modify: `src/electricity_app/cli.py`
- Modify: `tests/test_cli.py`
- Create: `deploy/electricity-backup.service`
- Create: `deploy/electricity-backup.timer`
- Modify: `deploy/electricity-app.tmpfiles.conf`
- Modify: `README.md`
- Modify: `tests/test_deployment.py`

**Interfaces:**
- Produces: `electricity-admin disable-wechat REQUEST_ID`, `reset-property-auth`, and `backup-db BACKUP_DIRECTORY --retention-days 30`.

- [ ] **Step 1: Write failing CLI and asset tests**

Assert valid revocation succeeds, invalid/missing/already-disabled IDs exit nonzero, backup creates a mode-0600 SQLite-consistent copy and removes only expired managed backups, and systemd assets use a restricted user/directory, `UMask=0077`, daily persistence, and a concrete 30-day policy.

- [ ] **Step 2: Run red tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli.py tests/test_deployment.py -v`

- [ ] **Step 3: Implement commands, units, timer, and documentation**

Use SQLite `Connection.backup`, atomic destination replacement, and deterministic retention. Document installation, enablement, timer verification, restore integrity check, and retention.

- [ ] **Step 4: Run green tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli.py tests/test_deployment.py -v`

---

### Task 7: Full verification, report, and commits

**Files:**
- Create: `.superpowers/sdd/2026-07-29-electricity-analysis/final-fix-report.md`

- [ ] **Step 1: Run all locally feasible checks**

```powershell
& .venv\Scripts\python.exe -m pytest -v
& .venv\Scripts\python.exe -m pytest tests/test_frontend_browser.py -v
& .venv\Scripts\python.exe -m compileall -q src tests
& .venv\Scripts\python.exe -m pip wheel --no-deps --wheel-dir .superpowers\wheel-check .
& .venv\Scripts\python.exe -m pip install --no-deps --force-reinstall <built-wheel>
git diff --check
```

Also run `nginx -t`, `systemd-analyze verify`, and `shellcheck` only when those executables are locally available; otherwise record them as external verification.

- [ ] **Step 2: Self-review against every brief item**

Map each Critical/Important/security-minor finding to exact files, red/green tests, and final commands. Confirm no credentials, response bodies, room details, device details, tokens, OAuth values, or personal identifiers appear in diagnostics.

- [ ] **Step 3: Write the required report and commit all changes**

Commit logical fix groups, list commit hashes in the report, and leave the worktree clean.
