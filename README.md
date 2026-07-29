# 园区电量分析 H5

这是一个部署在 Ubuntu 服务器上的只读电量分析服务。它从已获授权的物业接口读取单个房间/电表数据，写入 SQLite，自动计算电量、费用和余额趋势，并通过微信公众号菜单打开 H5 仪表盘。

服务启动后会立即同步一次数据，随后在每小时的 `00`、`30` 分钟执行同步；每天 `02:15` 做一次近 30 天数据补偿。服务不包含小程序前端，也不发送主动消息。

## 目录与每个文件的作用

```text
BE_Analyze/
├── .gitignore
├── README.md
└── backend/
    ├── .env.example
    ├── pyproject.toml
    ├── deploy/electricity-app.service
    ├── src/electricity_app/
    └── tests/
```

### 仓库根目录

| 文件 | 作用 |
| --- | --- |
| `.gitignore` | 排除真实环境变量、SQLite 数据库、虚拟环境、缓存和日志，防止敏感信息进入 Git。 |
| `README.md` | 本文档：说明仓库结构和服务器部署流程。 |

### 后端配置与部署文件

| 文件 | 作用 |
| --- | --- |
| `backend/.env.example` | 环境变量示例。部署时参考它创建服务器上的 `/etc/electricity-app/electricity.env`；不要在仓库内创建或提交真实 `.env`。 |
| `backend/pyproject.toml` | Python 包定义、固定依赖版本、测试依赖、`electricity-admin` 命令入口，以及 H5 静态资源打包规则。 |
| `backend/deploy/electricity-app.service` | 唯一的 systemd 服务单元。以低权限 `electricity` 用户运行 Uvicorn，仅监听 `127.0.0.1:8000`，并由 systemd 创建数据库目录。 |

### 后端运行源码

| 文件 | 作用 |
| --- | --- |
| `backend/src/electricity_app/__init__.py` | Python 包标记文件。 |
| `analytics.py` | 计算今日/昨日用电、费用、余额、7/30 天趋势、峰值、异常和数据陈旧状态。 |
| `cli.py` | `electricity-admin` 管理命令：初始化数据库、查看/批准微信授权、探测物业字段、按日期同步和输出某日汇总。 |
| `config.py` | 读取并校验环境变量；强制物业接口和公网地址使用 HTTPS，并限制会话、Token 等敏感配置。 |
| `db.py` | SQLite 表初始化、采集记录写入、余额快照、汇总数据、同步状态、OAuth nonce 与微信授权白名单读写。 |
| `domain.py` | 电量记录、同步结果、仪表盘统计等不可变数据结构。 |
| `main.py` | 组装 FastAPI、数据库、物业客户端、分析服务、调度器、Cookie 会话与敏感日志脱敏规则。 |
| `property_client.py` | 已授权物业接口的 HTTPS 调用、字段识别、数据解析和认证错误识别。 |
| `scheduler.py` | 应用内 APScheduler：启动同步、每 30 分钟同步和每日补偿同步；避免重叠运行。 |
| `sync_service.py` | 调用物业客户端、写入原始记录和同步结果，并在认证异常时暂停后续采集。 |
| `web.py` | 微信消息回调校验、网页 OAuth、H5 授权、仪表盘 API、健康检查和日志查询参数脱敏。 |

### H5 静态资源

| 文件 | 作用 |
| --- | --- |
| `backend/src/electricity_app/static/dashboard.html` | 电量仪表盘的唯一 HTML 页面。 |
| `backend/src/electricity_app/static/app.css` | H5 页面布局、移动端适配和图表卡片样式。 |
| `backend/src/electricity_app/static/app.js` | 请求仪表盘 API、渲染统计数字和 ECharts 图表；遇到未授权状态时跳转至微信 OAuth。 |

### 自动化测试

| 文件 | 作用 |
| --- | --- |
| `backend/tests/conftest.py` | 共享测试配置、临时数据库和模拟物业客户端。 |
| `backend/tests/fixtures/property_details.json` | 脱敏的物业接口响应样本。 |
| `backend/tests/test_analytics.py` | 验证电量、费用、趋势、峰值、异常和陈旧判断。 |
| `backend/tests/test_cli.py` | 验证管理命令输出、同步、授权批准和参数错误处理。 |
| `backend/tests/test_config.py` | 验证环境变量格式、HTTPS 和敏感配置约束。 |
| `backend/tests/test_db.py` | 验证 SQLite 迁移、写入、查询、汇总和授权白名单。 |
| `backend/tests/test_frontend_browser.py` | 在浏览器环境中验证 H5 页面加载、图表数据和未授权跳转。 |
| `backend/tests/test_property_client.py` | 验证物业接口请求、房间/电表筛选、字段兼容和认证错误。 |
| `backend/tests/test_scheduler.py` | 验证启动同步、30 分钟调度、补偿任务及应用生命周期。 |
| `backend/tests/test_sync_service.py` | 验证同步写入、失败处理和认证暂停逻辑。 |
| `backend/tests/test_web.py` | 验证微信消息签名、OAuth、授权白名单、H5/API 路由和健康检查。 |

## 服务器部署（Ubuntu 24.04）

以下步骤假设：

- 域名已解析到服务器公网 IP；
- 云防火墙/安全组已放通 TCP `80`、`443`；
- 已获物业/开发方授权，只接入指定账户；
- 以可执行 `sudo` 的服务器管理员账户操作。

文中 `<domain>` 必须替换成实际域名；填写域名时不要包含 `https://` 或路径。

### 1. 安装系统依赖并创建服务用户

```bash
sudo apt update
sudo apt install --yes git python3.12-venv nginx certbot python3-certbot-nginx sqlite3 curl rsync

sudo useradd --system \
  --home-dir /var/lib/electricity-app \
  --shell /usr/sbin/nologin \
  electricity
```

若 `electricity` 用户已存在，第二条命令会报错；可跳过它。

### 2. 下载代码并安装 Python 依赖

```bash
sudo git clone https://github.com/SkiJiang/BE_Analyze.git /opt/electricity-app
sudo chown -R root:root /opt/electricity-app
sudo find /opt/electricity-app -type d -exec chmod 0755 {} +
sudo find /opt/electricity-app -type f -exec chmod 0644 {} +

sudo python3.12 -m venv /opt/electricity-app/.venv
sudo /opt/electricity-app/.venv/bin/pip install --upgrade pip
sudo /opt/electricity-app/.venv/bin/pip install /opt/electricity-app/backend
```

代码目录由 `root` 持有；应用运行用户只需要读取源码和进入目录的权限。

### 3. 创建生产环境变量文件

真实配置只放在 `/etc/electricity-app/electricity.env`：

```bash
sudo install -d -o root -g root -m 0750 /etc/electricity-app
sudo install -o root -g root -m 0600 /dev/null /etc/electricity-app/electricity.env
sudoedit /etc/electricity-app/electricity.env
```

在编辑器中填入以下内容。`REPLACE_ME` 必须替换为真实值，但不要将真实值提交到 Git、截图或聊天记录中。

```dotenv
PROPERTY_BASE_URL=https://物业接口域名
PROPERTY_USERNAME=REPLACE_ME
PROPERTY_PASSWORD=REPLACE_ME
PROPERTY_ROOM_NAME=REPLACE_ME
PROPERTY_DEVICE_NAME=REPLACE_ME

DATABASE_PATH=/var/lib/electricity-app/electricity.db
SESSION_SECRET=至少32位随机字符串
SESSION_MAX_AGE_SECONDS=1800
OPENID_HMAC_KEY=至少32位随机字符串

WECHAT_APP_ID=REPLACE_ME
WECHAT_APP_SECRET=REPLACE_ME
WECHAT_MESSAGE_TOKEN=至少16位随机字符串
PUBLIC_BASE_URL=https://<domain>

TIMEZONE=Asia/Shanghai
STALE_AFTER_MINUTES=90
```

生成两个独立的 32 字节随机密钥可使用：

```bash
openssl rand -hex 32
```

保存后确认环境文件没有被普通用户读取：

```bash
sudo stat -c '%a %U:%G %n' /etc/electricity-app/electricity.env
# 预期：600 root:root /etc/electricity-app/electricity.env
```

### 4. 安装并启动 systemd 服务

```bash
sudo install -o root -g root -m 0644 \
  /opt/electricity-app/backend/deploy/electricity-app.service \
  /etc/systemd/system/electricity-app.service

sudo systemctl daemon-reload
sudo systemctl enable --now electricity-app
sudo systemctl status electricity-app --no-pager
```

服务首次启动会创建 `/var/lib/electricity-app` 并初始化数据库。应用只会监听本机环回地址，不能直接用公网 IP 加端口访问。

检查本机健康状态：

```bash
curl -fsS http://127.0.0.1:8000/health/live
sudo ss -lntp | grep 8000
```

预期健康检查返回 `{"status":"live"}`，监听地址为 `127.0.0.1:8000`。

### 5. 配置 Nginx 反向代理

创建 `/etc/nginx/sites-available/electricity-app`：

```nginx
server {
    listen 80;
    server_name <domain>;

    client_max_body_size 1m;
    proxy_connect_timeout 5s;
    proxy_read_timeout 60s;
    proxy_send_timeout 60s;

    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # OAuth code/state 和公众号签名参数不写入访问日志。
    location = /wechat/callback {
        access_log off;
        proxy_pass http://127.0.0.1:8000;
    }

    location = /wechat/message {
        access_log off;
        proxy_pass http://127.0.0.1:8000;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

启用站点并检查语法：

```bash
sudo ln -sf /etc/nginx/sites-available/electricity-app /etc/nginx/sites-enabled/electricity-app
sudo nginx -t
sudo systemctl reload nginx
```

### 6. 申请 HTTPS 证书

在 DNS 已生效且 80 端口可访问的情况下执行：

```bash
sudo certbot --nginx -d <domain>
sudo systemctl reload nginx
curl -fsS https://<domain>/health/live
```

Certbot 会自动在 Nginx 配置中加入证书和 HTTP→HTTPS 跳转。之后微信接口、网页授权和自定义菜单都使用 `https://<domain>`。

### 7. 配置微信测试公众号

在微信测试号管理页填写：

| 配置项 | 填写内容 |
| --- | --- |
| 接口 URL | `https://<domain>/wechat/message` |
| 接口 Token | 与 `WECHAT_MESSAGE_TOKEN` 完全一致 |
| 网页授权域名 | `<domain>` |
| JS 接口安全域名 | `<domain>`（仅在后续使用微信 JS-SDK 时需要） |
| 自定义菜单“电量分析” | `https://<domain>/wechat/entry` |

“网页授权域名”位于 **网页账号 → 获取授权用户基本信息 → 修改**。它不是接口 URL；只填写裸域名，否则会出现 `redirect_uri` 域名不一致（10003）。

测试用户先关注测试号，再点击“电量分析”菜单。首次访问会出现：

```json
{"detail":"authorization pending","request_id":1}
```

管理员通过临时 systemd 服务读取 root 权限环境文件并批准对应请求：

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  --property=User=electricity \
  --property=Group=electricity \
  --property=WorkingDirectory=/opt/electricity-app \
  --property=EnvironmentFile=/etc/electricity-app/electricity.env \
  /opt/electricity-app/.venv/bin/electricity-admin list-pending

sudo systemd-run --quiet --wait --pipe --collect \
  --property=User=electricity \
  --property=Group=electricity \
  --property=WorkingDirectory=/opt/electricity-app \
  --property=EnvironmentFile=/etc/electricity-app/electricity.env \
  /opt/electricity-app/.venv/bin/electricity-admin enable-wechat <request_id>
```

批准后重新打开菜单即可进入 H5 仪表盘。撤销某个用户权限：

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  --property=User=electricity \
  --property=Group=electricity \
  --property=WorkingDirectory=/opt/electricity-app \
  --property=EnvironmentFile=/etc/electricity-app/electricity.env \
  /opt/electricity-app/.venv/bin/electricity-admin disable-wechat <request_id>
```

## 日常运维

### 查看服务和日志

```bash
sudo systemctl status electricity-app --no-pager
sudo journalctl -u electricity-app -f
sudo journalctl -u electricity-app -n 100 --no-pager
```

### 触发一次立即同步

重启应用会在启动后安排一次立即同步：

```bash
sudo systemctl restart electricity-app
sudo journalctl -u electricity-app -n 100 --no-pager
```

如物业账号重新登录后需要清除认证暂停状态：

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  --property=User=electricity \
  --property=Group=electricity \
  --property=WorkingDirectory=/opt/electricity-app \
  --property=EnvironmentFile=/etc/electricity-app/electricity.env \
  /opt/electricity-app/.venv/bin/electricity-admin reset-property-auth
sudo systemctl restart electricity-app
```

### 查看某日数据

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  --property=User=electricity \
  --property=Group=electricity \
  --property=WorkingDirectory=/opt/electricity-app \
  --property=EnvironmentFile=/etc/electricity-app/electricity.env \
  /opt/electricity-app/.venv/bin/electricity-admin summarize-date 2026-07-29
```

### 更新服务器代码

```bash
cd /opt/electricity-app
sudo git pull --ff-only origin main
sudo /opt/electricity-app/.venv/bin/pip install --no-deps --upgrade /opt/electricity-app/backend
sudo install -o root -g root -m 0644 \
  /opt/electricity-app/backend/deploy/electricity-app.service \
  /etc/systemd/system/electricity-app.service
sudo systemctl daemon-reload
sudo systemctl restart electricity-app
curl -fsS https://<domain>/health/live
```

若新增了 Python 依赖，不使用 `--no-deps` 再执行一次安装：

```bash
sudo /opt/electricity-app/.venv/bin/pip install --upgrade /opt/electricity-app/backend
```

## 故障排查

| 现象 | 检查与处理 |
| --- | --- |
| Nginx 返回 `502 Bad Gateway` | `sudo systemctl status electricity-app --no-pager`，再查看 `journalctl -u electricity-app -n 100 --no-pager`；通常是环境变量缺失、格式错误或服务未启动。 |
| 微信显示 `10003 redirect_uri` 不一致 | 在“获取授权用户基本信息”的网页授权域名中填写裸域名 `<domain>`。 |
| 微信显示 `10005` 或无 scope 权限 | 确认测试微信号已关注测试号并位于测试用户列表，重新从自定义菜单进入。 |
| 页面显示 `authorization pending` | 用 `electricity-admin list-pending` 查看请求号，再运行 `enable-wechat <request_id>`。 |
| 数据陈旧或没有新记录 | 查看服务日志；确认物业账号可用、房间/设备名称匹配、物业接口可访问。 |
| 服务无法进入工作目录 | 检查 `/opt/electricity-app` 及其父目录至少为 `0755`，使 `electricity` 用户能读取并进入。 |

## 安全要求

- 仅接入已获得书面或明确授权的物业账户和房间。
- 不提交 `/etc/electricity-app/electricity.env`、本地 `.env`、SQLite 数据库、Token 或密码。
- 真实密钥若曾出现在截图、聊天、日志或公开仓库中，应立即在对应平台轮换。
- 服务器只对公网暴露 Nginx 的 80/443；Uvicorn 必须保持 `127.0.0.1:8000` 监听。
