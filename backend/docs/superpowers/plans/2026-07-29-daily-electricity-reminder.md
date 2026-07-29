# Daily Electricity Reminder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send one authenticated WeChat test-template electricity reminder per authorized user at 20:00 Asia/Shanghai each day.

**Architecture:** Store an encrypted copy of each authorized user’s OpenID alongside its existing HMAC allowlist identity. A small WeChat client caches the official-account access token and a reminder service builds analytics data, sends the template payload, and records successful daily deliveries for idempotency.

**Tech Stack:** Python 3.12, FastAPI, APScheduler, SQLite, httpx, cryptography Fernet, pytest, respx.

## Global Constraints

- Preserve the existing OAuth allowlist and use its HMAC identity as the delivery idempotency key.
- Store raw OpenIDs only as Fernet ciphertext and never log OpenIDs, access tokens, AppSecret, encryption keys, or template payload secrets.
- Use `WECHAT_DAILY_TEMPLATE_ID` and `WECHAT_OPENID_ENCRYPTION_KEY` from the root-only environment file.
- Schedule daily sending at 20:00 Asia/Shanghai and do not mark a failed or stale-data send as delivered.
- Deploy only after the complete backend test suite passes.

---

### Task 1: Add configuration and encrypted recipient persistence

**Files:**
- Modify: `backend/src/electricity_app/config.py`, `backend/src/electricity_app/db.py`, `backend/src/electricity_app/web.py`, `backend/.env.example`
- Test: `backend/tests/test_config.py`, `backend/tests/test_db.py`, `backend/tests/test_web.py`

**Interfaces:**
- Produces `Settings.wechat_daily_template_id: str` and `Settings.wechat_openid_encryption_key: SecretStr`.
- Produces `Database.save_authorized_openid(openid_hmac: str, ciphertext: str) -> bool` and `Database.list_reminder_recipients() -> list[tuple[str, str]]`.

- [ ] **Step 1: Write failing persistence tests**

```python
def test_authorized_openid_ciphertext_is_saved_and_listed(db):
    assert db.save_authorized_openid("digest", "ciphertext") is True
    assert db.list_reminder_recipients() == [("digest", "ciphertext")]
```

- [ ] **Step 2: Run the focused tests and confirm missing methods fail**

Run: `.venv\\Scripts\\python.exe -m pytest tests\\test_db.py -q`

Expected: FAIL because `save_authorized_openid` and `list_reminder_recipients` do not exist.

- [ ] **Step 3: Add the migration and methods**

Add nullable `openid_ciphertext` to `wechat_allowlist`, create `daily_reminder_deliveries(day, openid_hmac, sent_at)` with a composite primary key, and list only enabled recipients with ciphertext.

- [ ] **Step 4: Encrypt on the authorized OAuth callback**

Use `cryptography.fernet.Fernet(settings.wechat_openid_encryption_key.get_secret_value())` to encrypt the OpenID before calling `save_authorized_openid`; unknown users remain pending without storing the raw OpenID.

- [ ] **Step 5: Verify focused tests pass**

Run: `.venv\\Scripts\\python.exe -m pytest tests\\test_config.py tests\\test_db.py tests\\test_web.py -q`

Expected: PASS.

### Task 2: Implement the template-message client and reminder service

**Files:**
- Create: `backend/src/electricity_app/wechat_template.py`, `backend/src/electricity_app/reminders.py`
- Modify: `backend/src/electricity_app/db.py`, `backend/pyproject.toml`
- Test: `backend/tests/test_wechat_template.py`, `backend/tests/test_reminders.py`

**Interfaces:**
- Produces `WeChatTemplateClient.send_daily_reminder(openid: str, data: dict[str, str]) -> None`.
- Produces `DailyReminderService.send_for_day(day: date) -> int` returning the count of successful deliveries.
- Produces `Database.reminder_was_sent(day: date, openid_hmac: str) -> bool` and `Database.record_reminder_sent(day: date, openid_hmac: str, sent_at: datetime) -> None`.

- [ ] **Step 1: Write failing HTTP contract tests**

```python
def test_template_client_fetches_token_once_and_posts_template_payload():
    client.send_daily_reminder("openid", {"room": "7号楼/8F/805"})
    assert token_requests == 1
    assert payload["template_id"] == settings.wechat_daily_template_id
    assert payload["touser"] == "openid"
```

- [ ] **Step 2: Run the client test and confirm import failure**

Run: `.venv\\Scripts\\python.exe -m pytest tests\\test_wechat_template.py -q`

Expected: FAIL because `wechat_template` does not exist.

- [ ] **Step 3: Implement token caching and send validation**

Fetch `https://api.weixin.qq.com/cgi-bin/token` with `grant_type=client_credential`, cache token until one minute before `expires_in`, post to `https://api.weixin.qq.com/cgi-bin/message/template/send`, and raise a sanitized exception for non-zero `errcode` or malformed JSON.

- [ ] **Step 4: Implement the reminder service**

Decrypt each enabled recipient, skip existing delivery rows and stale dashboards, compose the eight fixed template fields, send, then record only successful sends.

- [ ] **Step 5: Verify service behavior**

Run: `.venv\\Scripts\\python.exe -m pytest tests\\test_wechat_template.py tests\\test_reminders.py -q`

Expected: PASS with one delivery per user/day.

### Task 3: Wire scheduling, manual sending, documentation, and deployment

**Files:**
- Modify: `backend/src/electricity_app/main.py`, `backend/src/electricity_app/scheduler.py`, `backend/src/electricity_app/cli.py`, `backend/README.md`
- Test: `backend/tests/test_scheduler.py`, `backend/tests/test_cli.py`

**Interfaces:**
- Extends `create_scheduler(..., reminder_service: DailyReminderService)` with job id `daily_reminder` at hour `20`, minute `0`.
- Adds `electricity-admin send-reminder [--day YYYY-MM-DD]`.

- [ ] **Step 1: Write failing scheduler and CLI tests**

```python
assert scheduler.get_job("daily_reminder").trigger.fields[5].expressions
monkeypatch.setattr("sys.argv", ["electricity-admin", "send-reminder"])
assert sent_count == 1
```

- [ ] **Step 2: Run focused tests and confirm the job and command are absent**

Run: `.venv\\Scripts\\python.exe -m pytest tests\\test_scheduler.py tests\\test_cli.py -q`

Expected: FAIL because no `daily_reminder` job or `send-reminder` command exists.

- [ ] **Step 3: Add the 20:00 job and CLI command**

Inject one shared `DailyReminderService` into the scheduler and CLI. The scheduled job uses `date.today()` in the configured timezone; the CLI defaults to that same date and prints only `sent=<count>`.

- [ ] **Step 4: Document configuration and test-account setup**

Document the two new environment variables, the exact eight template fields, the required re-login to capture an encrypted OpenID, and the server command `electricity-admin send-reminder`.

- [ ] **Step 5: Run complete verification and publish**

Run: `.venv\\Scripts\\python.exe -m pytest -q`, `git diff --check`, commit, push `main`, update `/opt/electricity-app`, add only the two new environment variables, restart `electricity-app`, then run the CLI manual send once after the template ID is configured.
