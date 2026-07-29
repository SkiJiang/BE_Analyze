# Daily Template Message Implementation Plan

**Goal:** Send the configured WeChat test template once daily at 20:00 to each authorized account.

**Architecture:** Persist encrypted OpenIDs in the existing allowlist, then use a token-caching WeChat client and an idempotent reminder service. The payload uses the eight fields in the configured test template, including `request_url`.

## Tasks

1. Add required environment settings, database migration for encrypted OpenIDs and daily delivery records, and OAuth capture tests.
2. Add a token-caching template-message client plus reminder service tests for payload, stale data, and idempotency.
3. Add the 20:00 scheduler job and `electricity-admin send-reminder` command; update deployment documentation and verify locally before server deployment.
