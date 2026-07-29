# 805 Electricity Analysis

This repository installs a read-only electricity collection and analysis service on Ubuntu 24.04. The application runs as an unprivileged system user, with one Uvicorn worker bound only to `127.0.0.1:8000`. Nginx is the only public HTTP service.

The examples below use reserved placeholder domains and placeholder credentials. Replace them during provisioning; do not commit the populated environment file.

## Install on Ubuntu 24.04

Run these commands from an administrator account on the server.

### 1. Create the service account and install packages

```bash
sudo useradd --system \
  --home-dir /var/lib/electricity-app \
  --shell /usr/sbin/nologin \
  electricity

sudo apt update
sudo apt install --yes python3.12-venv nginx gettext-base sqlite3 curl rsync
```

If the `electricity` user already exists, verify that it is a system account with no interactive shell instead of creating it again.

### 2. Copy and install the application

Copy the repository contents to `/opt/electricity-app` using the deployment mechanism of your choice, then keep the code root-owned and readable by the service:

```bash
sudo install -d -o root -g root -m 0755 /opt/electricity-app
sudo rsync -a --delete \
  --exclude .git/ \
  --exclude .venv/ \
  --exclude .env \
  --exclude .pytest_cache/ \
  --exclude __pycache__/ \
  --exclude .superpowers/ \
  ./ /opt/electricity-app/
sudo chown -R root:root /opt/electricity-app

sudo python3.12 -m venv /opt/electricity-app/.venv
sudo /opt/electricity-app/.venv/bin/python -m pip install --upgrade pip
cd /opt/electricity-app
sudo /opt/electricity-app/.venv/bin/pip install .
```

The `--delete` flag removes files at the destination that are absent from the source. Confirm that `/opt/electricity-app` is the intended deployment directory before running it.

### 3. Provision the runtime environment

Create a root-owned secrets directory and edit the environment file without placing secrets in shell history:

```bash
sudo install -d -o root -g root -m 0750 /etc/electricity-app
sudo install -o root -g root -m 0600 /dev/null \
  /etc/electricity-app/electricity.env
sudoedit /etc/electricity-app/electricity.env
sudo chmod 0600 /etc/electricity-app/electricity.env
sudo chown root:root /etc/electricity-app/electricity.env
```

Populate every value below. Values shown are non-working placeholders:

```dotenv
PROPERTY_BASE_URL=https://property-api.example.invalid
PROPERTY_USERNAME=REPLACE_ME
PROPERTY_PASSWORD=REPLACE_ME
PROPERTY_ROOM_NAME=麒麟科创园-7号楼-805
PROPERTY_DEVICE_NAME=7号楼/8F/805电表
DATABASE_PATH=/var/lib/electricity-app/electricity.db
SESSION_SECRET=REPLACE_WITH_AT_LEAST_32_RANDOM_CHARACTERS
SESSION_MAX_AGE_SECONDS=1800
OPENID_HMAC_KEY=REPLACE_WITH_AT_LEAST_32_RANDOM_CHARACTERS
WECHAT_APP_ID=REPLACE_ME
WECHAT_APP_SECRET=REPLACE_ME
PUBLIC_BASE_URL=https://electricity.example.com
POLL_MINUTES=30
TIMEZONE=Asia/Shanghai
STALE_AFTER_MINUTES=90
```

Generate the two independent 32-byte application secrets with a secure random generator. Store only the resulting values in the environment file. Verify the final ownership and mode:

```bash
sudo stat -c '%a %U:%G %n' /etc/electricity-app/electricity.env
```

The expected prefix is `600 root:root`.

### 4. Install tmpfiles and initialize SQLite

Install the tmpfiles rule before invoking the administrative CLI so the database directory exists with the correct owner:

```bash
sudo install -o root -g root -m 0644 \
  /opt/electricity-app/deploy/electricity-app.tmpfiles.conf \
  /etc/tmpfiles.d/electricity-app.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/electricity-app.conf
sudo stat -c '%a %U:%G %n' /var/lib/electricity-app
```

The expected prefixes are `750 electricity:electricity` for
`/var/lib/electricity-app` and `700 electricity:electricity` for
`/var/backups/electricity-app`. Use a transient service to load the root-only
environment file while running the CLI as the unprivileged service account:

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  --unit=electricity-init-db \
  --property=User=electricity \
  --property=Group=electricity \
  --property=WorkingDirectory=/opt/electricity-app \
  --property=EnvironmentFile=/etc/electricity-app/electricity.env \
  /opt/electricity-app/.venv/bin/electricity-admin init-db
```

### 5. Install the systemd service

```bash
sudo install -o root -g root -m 0644 \
  /opt/electricity-app/deploy/electricity-app.service \
  /etc/systemd/system/electricity-app.service
sudo install -o root -g root -m 0644 \
  /opt/electricity-app/deploy/electricity-backup.service \
  /etc/systemd/system/electricity-backup.service
sudo install -o root -g root -m 0644 \
  /opt/electricity-app/deploy/electricity-backup.timer \
  /etc/systemd/system/electricity-backup.timer
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/electricity-app.service
sudo systemd-analyze verify \
  /etc/systemd/system/electricity-backup.service \
  /etc/systemd/system/electricity-backup.timer
sudo systemctl enable --now electricity-app
sudo systemctl enable --now electricity-backup.timer
sudo systemctl status electricity-app
sudo systemctl status electricity-backup.timer
```

Do not add a public firewall rule for port 8000. Confirm that Uvicorn has only a loopback listener:

```bash
ss -lnt | grep ':8000'
```

### 6. Access the server before a domain exists

Until the formal domain and certificate are ready, keep Nginx unpublished and use SSH port forwarding from the administrator workstation:

```bash
ssh -N -L 8000:127.0.0.1:8000 ubuntu@47.85.202.202
```

In a second local terminal, check the process through the tunnel:

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/health/live
```

This tunnel is for private operational checks only. WeChat OAuth and secure session cookies require the formal HTTPS origin. Keep TCP 8000 closed publicly.

### 7. Configure the formal HTTPS origin and Nginx

After the domain is available:

1. Point its DNS record to `47.85.202.202`.
2. Obtain a valid certificate and private key for that hostname.
3. Set `PUBLIC_BASE_URL` in `/etc/electricity-app/electricity.env` to the final `https://` origin.
4. Render the Nginx template using only its three deployment substitutions.

```bash
export PUBLIC_HOST='electricity.example.com'
export TLS_CERTIFICATE='/etc/ssl/example/fullchain.pem'
export TLS_CERTIFICATE_KEY='/etc/ssl/example/privkey.pem'

envsubst '${PUBLIC_HOST} ${TLS_CERTIFICATE} ${TLS_CERTIFICATE_KEY}' \
  < /opt/electricity-app/deploy/nginx-electricity.conf.template \
  | sudo tee /etc/nginx/sites-available/electricity-app >/dev/null

sudo ln -sfn /etc/nginx/sites-available/electricity-app \
  /etc/nginx/sites-enabled/electricity-app
sudo test ! -L /etc/nginx/sites-enabled/default \
  || sudo unlink /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
sudo systemctl restart electricity-app
```

Replace all `example` values before enabling the site. Permit only SSH, HTTP, and HTTPS at the host or cloud firewall; Nginx proxies HTTPS traffic to loopback Uvicorn. The template redirects HTTP to HTTPS, rate-limits OAuth and API routes, denies dotfiles and `/admin` paths, and applies HSTS only in the HTTPS server.

In the WeChat Official Account console:

1. Configure the web authorization domain as the formal hostname, without a scheme or path.
2. Set the menu link to the formal HTTPS origin followed by `/wechat/entry`.
3. Confirm that the callback origin exactly matches `PUBLIC_BASE_URL`.

Do not enable the menu until DNS, TLS, Nginx validation, and the live health check all succeed.

### 8. Approve the first WeChat request

After the first user follows the menu and reaches the pending page, list pending requests. The command prints only the numeric request ID and creation time:

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  --unit=electricity-list-pending \
  --property=User=electricity \
  --property=Group=electricity \
  --property=WorkingDirectory=/opt/electricity-app \
  --property=EnvironmentFile=/etc/electricity-app/electricity.env \
  /opt/electricity-app/.venv/bin/electricity-admin list-pending
```

Enable the selected numeric request ID:

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  --unit=electricity-enable-wechat \
  --property=User=electricity \
  --property=Group=electricity \
  --property=WorkingDirectory=/opt/electricity-app \
  --property=EnvironmentFile=/etc/electricity-app/electricity.env \
  /opt/electricity-app/.venv/bin/electricity-admin enable-wechat REQUEST_ID
```

Revoke an enabled request with the local-only command below. A missing,
invalid, or already-disabled request exits nonzero:

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  --unit=electricity-disable-wechat \
  --property=User=electricity \
  --property=Group=electricity \
  --property=WorkingDirectory=/opt/electricity-app \
  --property=EnvironmentFile=/etc/electricity-app/electricity.env \
  /opt/electricity-app/.venv/bin/electricity-admin disable-wechat REQUEST_ID
```

### 9. Run deployment smoke checks

Run the script on the server after exporting the formal hostname:

```bash
export PUBLIC_HOST='electricity.example.com'
sudo --preserve-env=PUBLIC_HOST \
  bash /opt/electricity-app/deploy/smoke-test.sh
```

The smoke test checks both live endpoints, confirms unauthenticated API access returns HTTP 401, and proves port 8000 is not listening on all interfaces.

## Backup and restore

`electricity-backup.timer` runs daily with a randomized delay and catches up
after downtime. Its oneshot service calls SQLite's online
`Connection.backup` API, writes with `UMask=0077`, and keeps exactly the
managed `electricity-YYYYMMDDTHHMMSSZ.db` files whose modification time is
within the configured 30-day retention window. It never deletes unrelated
files. Verify both the schedule and a manual run:

```bash
sudo systemctl list-timers electricity-backup.timer
sudo systemctl start electricity-backup.service
sudo systemctl status electricity-backup.service
sudo journalctl -u electricity-backup.service --since today
sudo find /var/backups/electricity-app -maxdepth 1 -type f \
  -name 'electricity-*.db' -printf '%m %u:%g %f\n'
```

Expected backup file mode and owner are `600 electricity:electricity`. Copy
backups to separate, access-controlled storage and test restores regularly.

To restore, stop the service first, preserve the current database, restore the selected backup, and check database integrity before restarting:

```bash
sudo systemctl stop electricity-app
sudo -u electricity cp /var/lib/electricity-app/electricity.db \
  /var/lib/electricity-app/electricity.db.pre-restore
sudo -u electricity sqlite3 /var/lib/electricity-app/electricity.db \
  ".restore '/var/backups/electricity-app/electricity-YYYYMMDDTHHMMSSZ.db'"
sudo -u electricity sqlite3 /var/lib/electricity-app/electricity.db \
  'PRAGMA integrity_check;'
sudo systemctl start electricity-app
```

Proceed only when `PRAGMA integrity_check` returns `ok`. If it does not, leave the service stopped and recover the pre-restore database.

## Routine validation

After any deployment change:

```bash
sudo nginx -t
sudo systemd-analyze verify /etc/systemd/system/electricity-app.service
sudo journalctl -u electricity-app --since today
```

Never paste the populated environment file or authentication material into tickets, logs, or source control.

If synchronization enters `auth_required`, scheduled runs retain the status
but do not attempt another property login. After correcting the root-owned
credential environment, deliberately clear the running process gate:

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  --unit=electricity-reset-property-auth \
  --property=User=electricity \
  --property=Group=electricity \
  --property=WorkingDirectory=/opt/electricity-app \
  --property=EnvironmentFile=/etc/electricity-app/electricity.env \
  /opt/electricity-app/.venv/bin/electricity-admin reset-property-auth
```

Restarting `electricity-app` also clears this process-attempt gate. Neither
operation prints or logs credentials.

## Production acceptance — 2026-07-29 (pending)

Production acceptance is **not complete**. The repository was checked locally
with Python 3.12, placeholder configuration, a temporary SQLite database, and
no property or WeChat network access. Local automated checks cover the
following readiness boundaries:

- unauthenticated dashboard access is rejected with HTTP 401;
- an OAuth identity not enabled in the HMAC allowlist receives HTTP 403, an
  enabled numeric request permits access, and revocation is checked again on
  the next request;
- callback query values and structured values whose keys contain `openid`,
  `code`, `password`, `token`, `cookie`, or `authorization` are redacted;
- current-day plus previous-day synchronization, idempotent upserts, failure
  history preservation, 30-minute and daily job registration, single-instance
  coalescing, and the 90-minute stale-data warning are covered by tests;
- the systemd unit binds Uvicorn to `127.0.0.1:8000`, and the deployment smoke
  script rejects an unauthenticated dashboard and a wildcard port 8000
  listener.

The authorized one-day comparison commands are:

```bash
electricity-admin probe-property-schema
electricity-admin sync-date 2026-07-29
electricity-admin summarize-date 2026-07-29
```

`sync-date` prints only the date, status, record counts, and a sanitized error
class when it fails. `summarize-date` prints the record count, total energy,
total cost, latest balance, and energy/cost totals for each populated
half-hour bucket. Neither command prints room names, device names, upstream
record identifiers, credentials, tokens, or raw response bodies. A failed
`sync-date` exits nonzero.

The following production acceptance items remain pending and require the
authorized operator or infrastructure:

- [ ] Configure real property credentials outside Git with debug logging
  disabled. Run the schema probe and confirm every required field without
  capturing sensitive values or raw responses.
- [ ] Run `sync-date` and `summarize-date` for one authorized date. Compare the
  record count, total energy, total cost, latest balance, and at least three
  half-hour buckets against the property mini program. If a value differs,
  first add a sanitized fixture and a failing regression test.
- [ ] On the Ubuntu host, confirm Uvicorn listens only on
  `127.0.0.1:8000`. Before the formal origin is ready, inspect it only through
  SSH port forwarding and do not expose an IP-based H5 endpoint.
- [ ] After the formal domain, DNS, certificate, and WeChat authorization
  domain are ready, render and enable Nginx, run `deploy/smoke-test.sh`, and
  confirm that only ports 22, 80, and 443 are externally reachable.
- [ ] Exercise live WeChat OAuth over the formal HTTPS origin: verify no
  session returns 401, a disabled request returns 403, enabling its numeric
  request ID permits access, and disabling it revokes the next request.
- [ ] Inspect production logs and confirm they contain no raw `openid`, OAuth
  code, access token, property password, property token, or response body.
- [ ] Run the service for a full 24 hours and record at least 48 scheduled
  opportunities. Confirm successful runs remain idempotent and failures
  preserve history while readiness reports a degraded state.
- [ ] During that run, restart the service once and confirm records remain,
  both recurring jobs are registered once, the startup sync is queued once,
  missed runs coalesce, and duplicate upstream records do not increase the
  database count.
- [ ] Verify the dashboard in WeChat-compatible browsers at 375×812 and
  414×896 with representative data and confirm there is no page-level
  horizontal overflow.
- [ ] Recompare daily totals with the property mini program after the 24-hour
  run. Only then configure the Official Account menu to the formal
  `/wechat/entry` URL and mark this section accepted with dated evidence.
