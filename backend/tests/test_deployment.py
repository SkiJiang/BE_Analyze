from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
DEPLOY = ROOT / "deploy"


def _render_nginx() -> str:
    rendered = (DEPLOY / "nginx-electricity.conf.template").read_text(
        encoding="utf-8"
    )
    replacements = {
        "${PUBLIC_HOST}": "electricity.example.test",
        "${TLS_CERTIFICATE}": "/etc/ssl/example/fullchain.pem",
        "${TLS_CERTIFICATE_KEY}": "/etc/ssl/example/privkey.pem",
    }
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _location_block(configuration: str, declaration: str) -> str:
    start = configuration.index(declaration)
    opening = configuration.index("{", start)
    depth = 0
    for index in range(opening, len(configuration)):
        if configuration[index] == "{":
            depth += 1
        elif configuration[index] == "}":
            depth -= 1
            if depth == 0:
                return configuration[opening + 1 : index]
    raise AssertionError(f"unclosed Nginx block: {declaration}")


def test_rendered_nginx_logs_only_path_and_suppresses_callback_logs():
    rendered = _render_nginx()
    log_format_match = re.search(
        r"log_format\s+electricity_privacy\s+(.+?);",
        rendered,
        flags=re.DOTALL,
    )
    assert log_format_match is not None
    log_format = log_format_match.group(1)

    assert "$uri" in log_format
    assert "$request_uri" not in log_format
    assert "$args" not in log_format
    assert "$http_referer" not in log_format
    assert (
        "access_log /var/log/nginx/electricity-access.log "
        "electricity_privacy;"
    ) in rendered
    assert "$request_uri" not in rendered
    assert "$args" not in rendered
    assert "$http_referer" not in rendered

    callback = _location_block(
        rendered,
        "location = /wechat/callback",
    )
    assert "access_log off;" in callback
    assert "error_log /dev/null emerg;" in callback
    assert 'Referrer-Policy "no-referrer"' in rendered

    message = _location_block(
        rendered,
        "location = /wechat/message",
    )
    assert "access_log off;" in message
    assert "proxy_pass http://127.0.0.1:8000;" in message


def test_backup_systemd_assets_are_daily_restricted_and_retain_thirty_days():
    service = (DEPLOY / "electricity-backup.service").read_text(
        encoding="utf-8"
    )
    timer = (DEPLOY / "electricity-backup.timer").read_text(
        encoding="utf-8"
    )
    tmpfiles = (DEPLOY / "electricity-app.tmpfiles.conf").read_text(
        encoding="utf-8"
    )

    assert "Type=oneshot" in service
    assert "User=electricity" in service
    assert "Group=electricity" in service
    assert "UMask=0077" in service
    assert "ProtectSystem=strict" in service
    assert "ProtectHome=true" in service
    assert "NoNewPrivileges=true" in service
    assert "ReadOnlyPaths=/var/lib/electricity-app" not in service
    assert (
        "ReadWritePaths=/var/lib/electricity-app "
        "/var/backups/electricity-app"
    ) in service
    assert (
        "electricity-admin backup-db /var/backups/electricity-app "
        "--retention-days 30"
    ) in service
    assert "OnCalendar=daily" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=" in timer
    assert (
        "d /var/backups/electricity-app 0700 electricity electricity -"
        in tmpfiles
    )
