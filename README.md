# 园区电表分析与微信通知服务

这是一个针对**单个已授权房间和电表**的只读用电分析服务。它从物业平台拉取电表明细，保存到本机 SQLite 数据库，生成用电统计，并通过微信公众号为获准用户提供仪表盘和每日模板消息。

项目部署在 Ubuntu 服务器上。FastAPI 应用只监听本机回环地址，由 Nginx 负责 HTTPS 和公网访问；所有物业账户、微信密钥和会话密钥均保存在服务器的环境文件中，不应写入仓库。

## 功能概览

- 定时采集指定房间、指定电表的用电明细、费用、费率和余额。
- 将原始记录、余额快照、同步历史、日汇总及微信授权状态保存在 SQLite。
- 每 30 分钟刷新最近两天的数据，每日回补最近 30 天，减少物业平台延迟或临时故障造成的数据缺口。
- 提供微信内 H5 仪表盘，展示余额、当日与历史用电、费用、峰值、趋势、异常和数据新鲜度。
- 使用微信网页授权和管理员白名单控制访问者；未获批准的用户无法查看仪表盘或接收通知。
- 每日 `23:30`（`Asia/Shanghai`）向已授权接收者发送一条用电摘要模板消息。
- 提供命令行管理工具，用于批准接收者、排障、按日同步、查看汇总和手动重发通知。

## 运行架构

```text
物业电表平台
    |
    | HTTPS 登录和明细查询
    v
PropertyClient -> SyncService -> SQLite 数据库 <- AnalyticsService
                                      |                 |
                                      |                 +-> H5 / JSON API
                                      |
微信 OAuth <-> FastAPI/Uvicorn <-> Nginx/HTTPS <-> 微信用户
                                      |
                                      +-> 微信模板消息 API
```

生产进程由 `systemd` 维护：

```text
Nginx (80/443)
  -> Uvicorn/FastAPI (127.0.0.1:8000)
      -> APScheduler
          -> 电表同步任务
          -> 每日微信通知任务
```

服务以低权限 `electricity` 用户运行。源码目录为 `/opt/electricity-app`，运行时数据库目录为 `/var/lib/electricity-app`，敏感环境变量文件为 `/etc/electricity-app/electricity.env`。

## 目录结构

```text
/opt/electricity-app/
├── README.md                         本文档
├── .git/                             Git 元数据
└── backend/
    ├── .env.example                  环境变量示例，不含真实密钥
    ├── pyproject.toml                Python 包、依赖和 CLI 入口
    ├── uv.lock                       锁定的 Python 依赖
    ├── deploy/
    │   └── electricity-app.service   systemd 服务单元模板
    ├── src/electricity_app/
    │   ├── main.py                   FastAPI 应用组装与生命周期
    │   ├── config.py                 环境变量读取与校验
    │   ├── api_transport.py          外部 HTTP 重试、解析和错误分类
    │   ├── property_client.py        物业接口认证和数据抓取
    │   ├── sync_service.py           同步、失败记录和认证熔断
    │   ├── db.py                     SQLite schema 与持久化
    │   ├── analytics.py              用电、费用与趋势统计
    │   ├── scheduler.py              APScheduler 定时任务
    │   ├── reminders.py              每日模板消息编排
    │   ├── wechat_template.py        微信 access token 与模板消息调用
    │   ├── web.py                    OAuth、H5/API、健康检查
    │   ├── cli.py                    electricity-admin 管理命令
    │   └── static/                   仪表盘 HTML、CSS、JavaScript
    └── tests/                        单元、接口和浏览器测试
```

## 数据采集与同步

### 采集范围

应用不尝试抓取物业账户下的所有设备。每条记录都会校验房间名称和电表名称，必须分别与 `PROPERTY_ROOM_NAME` 和 `PROPERTY_DEVICE_NAME` 完全匹配。发现范围不匹配、字段缺失或数值非法时，同步失败而不会将异常数据写入分析结果。

物业客户端采用 HTTPS，登录获得的物业 token 只在当前进程内存中保存。明细查询支持分页，单次查询最多 100 页；网络或服务端临时错误会有限次数重试。若 token 失效，会重新登录一次再重试请求。

账户余额来自物业官方的独立接口 `/goodits/count/getBalance` 中目标房间的 `powerMoney` 字段，而不是用电明细中的历史 `balance` 字段。物业余额接口返回的房间名称可能包含楼层段（例如 `8F`），客户端会进行受限的房间名称归一化后再匹配；如果无法唯一匹配目标房间，余额不会被错误写入。独立余额接口暂时不可用时，电量明细仍会正常保存，并保留明细余额作为回退。

### 定时任务

所有时间均为 `Asia/Shanghai`：

| 时间 | 任务 | 行为 |
| --- | --- | --- |
| 应用启动后 | 启动同步 | 同步昨天和今天的数据。 |
| 每小时 `00`、`30` 分 | `sync_recent` | 同步昨天和今天，处理跨天记录和延迟入账。 |
| 每日 `02:15` | `reconcile_30_days` | 重新同步最近 30 天，回补历史数据。 |
| 每日 `23:30` | `daily_reminder` | 向已完整授权的用户发送当天摘要。 |

同步任务之间使用进程内锁避免重叠执行。调度任务设置 `max_instances=1`、合并错过的任务，并允许 15 分钟的启动延迟容错。

### 失败与认证熔断

同步结果会记录为 `success`、`failed` 或 `auth_required`。

- 网络错误、协议变化或数据格式错误记录为 `failed`，之后的定时任务仍可再次尝试。
- 物业账号密码或 token 被拒绝时记录为 `auth_required`，并激活认证熔断标记，后续同步不会持续尝试登录。
- 管理员确认物业账号已恢复后，运行 `reset-property-auth` 清除熔断，再重启服务或等待下一轮同步。

## 数据库与统计口径

默认数据库位置由 `DATABASE_PATH` 指定，生产环境通常为：

```text
/var/lib/electricity-app/electricity.db
```

数据库中的主要表：

| 表 | 用途 |
| --- | --- |
| `electricity_records` | 物业返回的原始电表明细，以物业记录 ID 或内容哈希去重。 |
| `balance_snapshots` | 每次同步观测到的最新余额快照。 |
| `sync_runs` | 每次同步的时间、范围、状态、抓取数、新增数、更新数和安全错误码。 |
| `daily_summaries` | 按自然日重建的总电量、总费用、记录数、峰值和异常基线。 |
| `wechat_allowlist` | 微信用户的 HMAC 标识、加密 OpenID、启用状态和申请编号。 |
| `daily_reminder_deliveries` | 每位接收者每天的正常推送去重记录。 |
| `oauth_nonces` | 五分钟有效的一次性微信 OAuth 状态值摘要。 |
| `runtime_state` | 当前运行状态，例如物业认证熔断。 |

统计服务按半小时聚合数据，并在仪表盘中计算：

- 当前余额、当日用电量与费用；
- 昨日用电量及当日环比；
- 近 24 小时、7 天和 30 天的趋势与费用；
- 最近完整日的平均用电、历史典型高峰时段和预计可用天数；
- 当天峰值半小时区间、按小时分布、最近 48 个半小时桶；
- 历史异常提示；
- 最近成功同步时间及数据是否陈旧。

当距最近一次成功同步超过 `STALE_AFTER_MINUTES`（当前实现固定为 90 分钟）时，数据被标记为陈旧。每日通知会在数据陈旧时跳过，以避免推送错误信息。

### 外部接口处理约定

物业、微信 OAuth 和微信模板消息都经过 `api_transport.py` 的统一 HTTP 传输层：

- 仅允许 GET/POST，并使用已配置的 TLS 校验；不通过关闭证书验证来规避错误。
- 网络/TLS 失败和上游 5xx 最多重试 3 次，退避间隔为 0.25 秒、0.5 秒；4xx 不重试。
- 响应必须是 JSON 对象，非法 JSON、非对象响应和上游业务错误会转换成安全的内部错误类别。
- 日志和异常不会包含请求体、密码、token、OAuth code、OpenID 或完整查询参数。
- 物业 token 失效时只允许重新登录一次；连续失败会进入认证熔断。
- 微信 access token 按过期时间缓存，并在到期前 60 秒刷新。

## Web 与微信授权

### HTTP 路由

| 路由 | 用途 | 权限 |
| --- | --- | --- |
| `GET /wechat/message` | 微信公众平台 URL 验证。 | 微信签名校验。 |
| `POST /wechat/message` | 接收微信服务器回调，目前仅返回成功。 | 无业务处理。 |
| `GET /wechat/entry` | 发起微信网页授权。 | 公开入口。 |
| `GET /wechat/callback` | 校验 OAuth state，交换 code 并建立会话。 | 微信 OAuth 回调。 |
| `GET /dashboard` | 仪表盘页面。 | 已批准用户。 |
| `GET /api/dashboard` | 仪表盘汇总 JSON。 | 已批准用户。 |
| `GET /api/day/{YYYY-MM-DD}` | 指定日期的半小时明细 JSON。 | 已批准用户。 |
| `GET /health/live` | 进程存活检查。 | 公开。 |
| `GET /health/ready` | 数据库、物业认证和数据新鲜度检查。 | 公开。 |

`/health/ready` 的含义：

- `200 {"status":"ready"}`：数据库可用，且没有已知的认证阻断。
- `200 {"status":"degraded"}`：服务可用，但已有成功同步且数据超过 90 分钟未更新。
- `503 {"status":"auth_required"}`：物业认证失败，等待管理员恢复认证。
- `503 {"status":"not_ready"}`：SQLite 无法正常读取。

### 授权与接收者流程

新用户必须在微信中访问：

```text
https://<你的域名>/wechat/entry
```

流程如下：

```text
用户访问入口
  -> 微信 OAuth
  -> 未获批准：写入待审批名单并返回 request_id
  -> 管理员 enable-wechat <request_id>
  -> 用户再次访问入口
  -> 保存加密 OpenID，建立会话，可访问仪表盘和接收消息
```

OpenID 不以明文索引保存：数据库中以 HMAC 作为身份标识；只有获批准用户再次完成授权时，才会将其 OpenID 用 Fernet 加密后保存，用于发送模板消息。OAuth `state` 同时受签名、五分钟过期、浏览器 session 和一次性数据库 nonce 保护。

## 微信每日通知

每天 `23:30`，服务会向已启用且已保存加密 OpenID 的接收者发送模板消息，内容包括：

- 电表/房间名称；
- 当前日期；
- 当日用电量和费用；
- 最新余额；
- 近 7 天用电量；
- 最近成功同步时间；
- 打开仪表盘的链接。

普通定时推送按“日期 + 接收者”去重。同一位用户当天已经收到过自动消息，下一次自动任务不会再发。管理员手动执行 `--force` 可明确绕过此限制，用于补发或验证模板消息；它不会删除任何历史投递记录。

## 配置

生产配置位于：

```text
/etc/electricity-app/electricity.env
```

参考 [`backend/.env.example`](backend/.env.example) 创建，真实值不得提交到 Git。示例：

```dotenv
PROPERTY_BASE_URL=https://property.example.com
PROPERTY_USERNAME=REPLACE_ME
PROPERTY_PASSWORD=REPLACE_ME
PROPERTY_ROOM_NAME=REPLACE_ME
PROPERTY_DEVICE_NAME=REPLACE_ME

DATABASE_PATH=/var/lib/electricity-app/electricity.db
SESSION_SECRET=REPLACE_WITH_AT_LEAST_32_CHARACTERS
SESSION_MAX_AGE_SECONDS=1800
OPENID_HMAC_KEY=REPLACE_WITH_AT_LEAST_32_CHARACTERS

WECHAT_APP_ID=wx0000000000000000
WECHAT_APP_SECRET=REPLACE_ME
WECHAT_MESSAGE_TOKEN=REPLACE_WITH_AT_LEAST_16_CHARACTERS
WECHAT_DAILY_TEMPLATE_ID=REPLACE_ME
WECHAT_OPENID_ENCRYPTION_KEY=REPLACE_WITH_A_FERNET_KEY
PUBLIC_BASE_URL=https://electricity.example.com

TIMEZONE=Asia/Shanghai
STALE_AFTER_MINUTES=90
```

| 变量 | 说明 |
| --- | --- |
| `PROPERTY_BASE_URL` | 已授权物业平台的 HTTPS 基础地址。 |
| `PROPERTY_USERNAME` / `PROPERTY_PASSWORD` | 物业平台登录凭据。 |
| `PROPERTY_ROOM_NAME` / `PROPERTY_DEVICE_NAME` | 允许采集的精确房间与电表名称。 |
| `DATABASE_PATH` | SQLite 数据库绝对路径。 |
| `SESSION_SECRET` | Web 会话签名密钥，至少 32 字符。 |
| `SESSION_MAX_AGE_SECONDS` | 登录 session 有效期，范围 300 到 3600 秒。 |
| `OPENID_HMAC_KEY` | 生成 OpenID HMAC 标识的密钥，至少 32 字符。 |
| `WECHAT_APP_ID` / `WECHAT_APP_SECRET` | 微信公众平台应用凭据。 |
| `WECHAT_MESSAGE_TOKEN` | 微信服务器回调校验 token，至少 16 字符。 |
| `WECHAT_DAILY_TEMPLATE_ID` | 每日摘要的模板消息 ID。 |
| `WECHAT_OPENID_ENCRYPTION_KEY` | Fernet 密钥，用于加密保存授权接收者 OpenID。 |
| `PUBLIC_BASE_URL` | 用户访问的 HTTPS 公网根地址，必须与微信授权域名一致。 |
| `TIMEZONE` | 当前仅支持 `Asia/Shanghai`。 |
| `STALE_AFTER_MINUTES` | 数据陈旧阈值，当前实现限定为 90。 |

建议权限：

```bash
sudo install -d -o root -g root -m 0750 /etc/electricity-app
sudo chown root:root /etc/electricity-app/electricity.env
sudo chmod 0600 /etc/electricity-app/electricity.env
```

## systemd 与反向代理

服务单元安装路径：

```text
/etc/systemd/system/electricity-app.service
```

核心运行参数：

```text
User=electricity
WorkingDirectory=/opt/electricity-app/backend
EnvironmentFile=/etc/electricity-app/electricity.env
ExecStart=/usr/local/bin/uv run --project /opt/electricity-app/backend --no-sync \
  uvicorn electricity_app.main:app --host 127.0.0.1 --port 8000 --workers 1 \
  --proxy-headers --forwarded-allow-ips=127.0.0.1
```

服务使用 `PrivateTmp`、`ProtectSystem=strict`、`ProtectHome`、`NoNewPrivileges` 和 `RestrictSUIDSGID` 等隔离选项。不要把 Uvicorn 改成对公网监听；应始终通过 Nginx 提供 TLS。

Nginx 至少应将 `Host`、`X-Forwarded-For` 和 `X-Forwarded-Proto` 转发给本机 `127.0.0.1:8000`。对 `/wechat/callback` 与 `/wechat/message` 建议关闭 Nginx 访问日志，防止 OAuth code、state 或微信签名参数落入日志。

## 管理命令

以下命令使用当前生产环境配置运行。`sudo bash -c` 会在 root shell 中读取权限为 `0600` 的环境文件，然后启动命令；命令本身不会打印密钥。

先定义一个便于复制的命令前缀：

```bash
sudo bash -c 'set -a; . /etc/electricity-app/electricity.env; set +a; \
  /usr/local/bin/uv run --project /opt/electricity-app/backend --no-sync electricity-admin <command>'
```

将 `<command>` 替换为下列内容。

| 命令 | 作用 |
| --- | --- |
| `init-db` | 初始化或迁移 SQLite 表结构。 |
| `list-pending` | 列出待批准微信用户的申请编号和创建时间。 |
| `enable-wechat <request_id>` | 批准一个申请。用户之后必须再次打开授权入口，才能保存接收消息所需的加密 OpenID。 |
| `disable-wechat <request_id>` | 禁用对应用户的仪表盘和后续通知权限。 |
| `send-daily-reminder` | 手动发送今日消息，但跳过当天已成功发送过的接收者。 |
| `send-daily-reminder --force` | 强制重发给全部有效接收者，忽略当天去重记录。 |
| `reset-property-auth` | 清除物业认证熔断标记。 |
| `probe-property-schema` | 请求当日物业数据并输出字段名和必要字段检测结果。 |
| `sync-date YYYY-MM-DD` | 同步指定自然日；同步失败会返回非零退出码。 |
| `summarize-date YYYY-MM-DD` | 输出指定日的记录数、总量、费用、余额和半小时桶。 |

常用示例：

```bash
# 查看待批准用户
sudo bash -c 'set -a; . /etc/electricity-app/electricity.env; set +a; \
  /usr/local/bin/uv run --project /opt/electricity-app/backend --no-sync electricity-admin list-pending'

# 批准申请编号 2
sudo bash -c 'set -a; . /etc/electricity-app/electricity.env; set +a; \
  /usr/local/bin/uv run --project /opt/electricity-app/backend --no-sync electricity-admin enable-wechat 2'

# 立即重发一条每日摘要
sudo bash -c 'set -a; . /etc/electricity-app/electricity.env; set +a; \
  /usr/local/bin/uv run --project /opt/electricity-app/backend --no-sync electricity-admin send-daily-reminder --force'

# 检查指定日期的汇总
sudo bash -c 'set -a; . /etc/electricity-app/electricity.env; set +a; \
  /usr/local/bin/uv run --project /opt/electricity-app/backend --no-sync electricity-admin summarize-date 2026-08-13'
```

## 日常运维

### 服务状态与日志

```bash
sudo systemctl status electricity-app --no-pager
sudo journalctl -u electricity-app -n 100 --no-pager
sudo journalctl -u electricity-app -f
```

### 重启和健康检查

重启服务会重新加载代码和环境变量，并在启动后安排一次最近两天的同步：

```bash
sudo systemctl restart electricity-app
sudo systemctl is-active electricity-app
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:8000/health/ready
```

### 手动恢复物业认证

确认物业账号、密码或接口恢复后：

```bash
sudo bash -c 'set -a; . /etc/electricity-app/electricity.env; set +a; \
  /usr/local/bin/uv run --project /opt/electricity-app/backend --no-sync electricity-admin reset-property-auth'
sudo systemctl restart electricity-app
```

### 备份和恢复数据库

在修改、迁移或升级前备份数据库。推荐使用 SQLite 在线备份命令：

```bash
sudo install -d -o root -g root -m 0700 /var/backups/electricity-app
sudo sqlite3 /var/lib/electricity-app/electricity.db \
  ".backup '/var/backups/electricity-app/electricity-$(date +%F-%H%M%S).db'"
```

恢复前先停止服务，并明确指定一个已验证的备份文件：

```bash
sudo systemctl stop electricity-app
sudo install -o electricity -g electricity -m 0600 \
  /var/backups/electricity-app/<backup-file>.db \
  /var/lib/electricity-app/electricity.db
sudo systemctl start electricity-app
```

### 更新代码

更新前请检查工作区状态，避免覆盖本机尚未提交的修改：

```bash
cd /opt/electricity-app
git status --short
```

确认工作区可更新后：

```bash
sudo git -C /opt/electricity-app pull --ff-only origin main
sudo /usr/local/bin/uv sync --project /opt/electricity-app/backend --locked
sudo install -o root -g root -m 0644 \
  /opt/electricity-app/backend/deploy/electricity-app.service \
  /etc/systemd/system/electricity-app.service
sudo systemctl daemon-reload
sudo systemctl restart electricity-app
curl -fsS http://127.0.0.1:8000/health/ready
```

## 开发与测试

项目要求 Python 3.12 及以上，依赖由 `uv` 管理。

```bash
cd /opt/electricity-app/backend
/usr/local/bin/uv sync --locked --extra test
/usr/local/bin/uv run pytest
```

针对本次改动运行相关测试：

```bash
/usr/local/bin/uv run pytest tests/test_scheduler.py tests/test_reminders.py
```

开发时可以用独立的临时数据库和测试微信凭据运行应用，禁止直接使用生产环境文件。若需要本地启动：

```bash
cd /opt/electricity-app/backend
set -a
. .env
set +a
/usr/local/bin/uv run uvicorn electricity_app.main:app --host 127.0.0.1 --port 8000
```

## 故障排查

| 现象 | 优先检查项 | 处理建议 |
| --- | --- | --- |
| Nginx 返回 `502` | `systemctl status` 和应用日志 | 检查服务是否启动、环境变量是否通过校验、端口是否为 `127.0.0.1:8000`。 |
| `/health/ready` 返回 `auth_required` | 最近日志和物业账户状态 | 恢复物业凭据后运行 `reset-property-auth`，再重启服务。 |
| 仪表盘显示数据陈旧 | `sync_runs`、服务日志、物业网络 | 物业接口不可达或同步失败时先处理根因；不要仅靠重启掩盖失败。 |
| 微信显示 `authorization pending` | `list-pending` 输出 | 批准对应 `request_id`，并让用户再次打开 `/wechat/entry`。 |
| 已批准用户收不到消息 | 是否已再次授权、模板 ID、微信关注状态、数据是否陈旧 | 用户必须在批准后再次进入授权入口；必要时用 `send-daily-reminder --force` 检查。 |
| 微信 OAuth 显示 `redirect_uri` 错误 | 微信网页授权域名和 `PUBLIC_BASE_URL` | 两者必须使用同一 HTTPS 域名；授权域名填写裸域名，不包含路径。 |
| 定时通知未在 23:30 发送 | 服务运行状态、时区和日志 | 确认系统时间与 `Asia/Shanghai`，检查进程启动后是否成功加载 scheduler。 |
| 物业记录范围不匹配 | `PROPERTY_ROOM_NAME`、`PROPERTY_DEVICE_NAME` | 使用 `probe-property-schema` 核对物业返回字段和名称，避免放宽范围校验。 |

## 安全要求

- 只连接已获得明确授权的物业账户、房间和电表。
- 真实密码、token、OpenID、微信 App Secret、Fernet 密钥和 session 密钥绝不进入 Git、截图、工单或聊天记录。
- 如密钥曾暴露，应立即在对应的物业或微信平台轮换；仅修改代码不能使已泄露的密钥失效。
- `electricity.env` 应保持 root 所有、`0600` 权限；数据库和备份也应视为敏感数据。
- Uvicorn 只监听 `127.0.0.1`，公网只开放 Nginx 的 `80/443`；生产必须使用 HTTPS。
- OAuth 回调和微信回调的查询参数不应写入代理访问日志。应用自身已对日志中的常见敏感字段做脱敏，但不应依赖日志脱敏代替权限控制。
- 手动 `--force` 会导致重复消息，仅在明确需要补发或验证时使用。
