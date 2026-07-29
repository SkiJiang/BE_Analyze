# 电量分析 H5

一个只读的园区电量采集与分析服务。数据由应用自身每 30 分钟同步一次，微信公众号菜单打开 H5 仪表盘。

## 目录

```text
backend/
├── .env.example
├── deploy/electricity-app.service
├── pyproject.toml
├── src/electricity_app/
│   ├── static/              # 唯一 H5 页面、样式和脚本
│   └── *.py
└── tests/
```

## 本地运行

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\pip.exe install -e ".[test]"
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\uvicorn.exe electricity_app.main:app --host 127.0.0.1 --port 8000
```

在 `backend/.env` 填入物业接口和微信配置。此文件不得提交：

```dotenv
PROPERTY_BASE_URL=https://property.example.com
PROPERTY_USERNAME=REPLACE_ME
PROPERTY_PASSWORD=REPLACE_ME
PROPERTY_ROOM_NAME=REPLACE_ME
PROPERTY_DEVICE_NAME=REPLACE_ME
DATABASE_PATH=/var/lib/electricity-app/electricity.db
SESSION_SECRET=AT_LEAST_32_RANDOM_CHARACTERS
OPENID_HMAC_KEY=AT_LEAST_32_RANDOM_CHARACTERS
WECHAT_APP_ID=REPLACE_ME
WECHAT_APP_SECRET=REPLACE_ME
WECHAT_MESSAGE_TOKEN=AT_LEAST_16_RANDOM_CHARACTERS
PUBLIC_BASE_URL=https://<domain>
TIMEZONE=Asia/Shanghai
STALE_AFTER_MINUTES=90
```

## Ubuntu 24.04 部署

以下命令以 `<domain>` 表示已解析到服务器公网 IP 的域名。

```bash
sudo useradd --system --home-dir /var/lib/electricity-app --shell /usr/sbin/nologin electricity
sudo apt update
sudo apt install --yes python3.12-venv nginx certbot python3-certbot-nginx sqlite3

sudo install -d -o root -g root -m 0755 /opt/electricity-app
sudo rsync -a --delete --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
  --exclude .venv/ --exclude .env ./backend/ /opt/electricity-app/
sudo python3.12 -m venv /opt/electricity-app/.venv
sudo /opt/electricity-app/.venv/bin/pip install /opt/electricity-app

sudo install -d -o root -g root -m 0750 /etc/electricity-app
sudo install -o root -g root -m 0600 /dev/null /etc/electricity-app/electricity.env
sudoedit /etc/electricity-app/electricity.env

sudo install -o root -g root -m 0644 /opt/electricity-app/deploy/electricity-app.service /etc/systemd/system/electricity-app.service
sudo systemctl daemon-reload
sudo systemctl enable --now electricity-app
```

Nginx 站点文件 `/etc/nginx/sites-available/electricity-app`：

```nginx
server {
    listen 80;
    server_name <domain>;

    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    location = /wechat/callback { access_log off; proxy_pass http://127.0.0.1:8000; }
    location = /wechat/message  { access_log off; proxy_pass http://127.0.0.1:8000; }
    location / { proxy_pass http://127.0.0.1:8000; }
}
```

启用 HTTPS：

```bash
sudo ln -sf /etc/nginx/sites-available/electricity-app /etc/nginx/sites-enabled/electricity-app
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d <domain>
curl -fsS https://<domain>/health/live
```

## 微信测试公众号

- 消息接口 URL：`https://<domain>/wechat/message`
- 消息 Token：环境变量 `WECHAT_MESSAGE_TOKEN`
- 网页授权域名：`<domain>`（仅域名，不带协议和路径）
- 自定义菜单链接：`https://<domain>/wechat/entry`

首次授权显示 `authorization pending` 时，在服务器批准该请求：

```bash
sudo -u electricity /opt/electricity-app/.venv/bin/electricity-admin list-pending
sudo -u electricity /opt/electricity-app/.venv/bin/electricity-admin enable-wechat <request_id>
```

## 运维

```bash
sudo systemctl status electricity-app --no-pager
sudo journalctl -u electricity-app -f
sudo systemctl restart electricity-app
curl -fsS https://<domain>/health/live
```

应用内置调度器会在启动后立即同步，并在每小时的 `00` 与 `30` 分钟继续同步。真实凭据只保存在 `/etc/electricity-app/electricity.env`；如凭据曾出现在截图、聊天或日志中，应立即轮换。
