# 园区电量分析（BE_Analyze）

面向单个已获授权房间的只读电量采集、用量分析和微信入口项目。系统按 30 分钟同步一次物业侧数据，将原始读数和分析结果保存到本地 SQLite，并提供 FastAPI H5 页面、微信测试公众号网页授权及消息回调。

> 安全边界：仅接入已获物业/开发方明确授权的账户。不要将物业账号密码、微信 AppSecret、回调 Token、会话密钥或生产数据库提交到 Git。若任一密钥曾出现在截图、终端记录或聊天中，请在对应平台立即轮换。

## 项目结构

```
BE_Analyze/
├── backend/                 # FastAPI 服务、采集器、SQLite、systemd 与 Nginx 配置
├── miniprogram/             # 微信开发者工具创建的小程序前端工程
├── typings/                 # 小程序 TypeScript 类型定义
├── project.config.json      # 小程序项目配置
└── package.json             # 小程序工具链配置
```

后端的完整运维参考在 [backend/README.md](backend/README.md)。本 README 给出从本地验证到公网部署、再到接入微信测试公众号的完整流程。

## 一、准备条件

- Windows 本地开发：Python 3.12、Git、微信开发者工具。
- 公网服务器：Ubuntu 24.04、可访问的域名、80/443 端口已放通。
- 已授权的物业系统访问凭据，以及对应房间名称/设备名称。
- 微信测试公众号（或已认证公众号）的 AppID、AppSecret；测试用户需要先关注测试号。

## 二、本地启动与验证

在 PowerShell 中执行：

```powershell
cd C:\Users\zdht\Desktop\BE_Analyze\backend
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -e .
Copy-Item .env.example .env
```

编辑 `backend/.env`，填写实际值。该文件仅留在本机，禁止提交：

```dotenv
PROPERTY_BASE_URL=https://物业接口域名
PROPERTY_USERNAME=物业账号
PROPERTY_PASSWORD=物业密码
PROPERTY_ROOM_NAME=授权房间名称
PROPERTY_DEVICE_NAME=授权电表名称
DATABASE_PATH=./electricity.db
SESSION_SECRET=至少32位随机字符串
OPENID_HMAC_KEY=至少32位随机字符串
WECHAT_APP_ID=测试号或公众号AppID
WECHAT_APP_SECRET=测试号或公众号AppSecret
WECHAT_MESSAGE_TOKEN=至少16位随机字符串
PUBLIC_BASE_URL=https://你的公网域名
POLL_MINUTES=30
TIMEZONE=Asia/Shanghai
STALE_AFTER_MINUTES=90
```

初始化数据库、立即同步一次并启动开发服务：

```powershell
.\.venv\Scripts\electricity-admin.exe init-db
.\.venv\Scripts\electricity-admin.exe sync-now
.\.venv\Scripts\uvicorn.exe electricity_app.web:app --host 127.0.0.1 --port 8000
```

另开一个窗口检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live
.\.venv\Scripts\python.exe -m pytest -q
```

## 三、部署到 Ubuntu 24.04

以下示例使用 `<domain>` 代表已备案并解析到服务器公网 IP 的域名。生产环境不要通过 IP 地址承载微信 H5 回调。

### 1. 安装系统依赖和服务用户

```bash
sudo useradd --system --home-dir /var/lib/electricity-app \
  --shell /usr/sbin/nologin electricity
sudo apt update
sudo apt install --yes python3.12-venv nginx gettext-base sqlite3 curl rsync certbot python3-certbot-nginx
```

### 2. 上传并安装后端

将仓库克隆到服务器后，只部署 `backend/`：

```bash
sudo install -d -o root -g root -m 0755 /opt/electricity-app
sudo rsync -a --delete \
  --exclude .git/ --exclude .venv/ --exclude .env \
  --exclude .pytest_cache/ --exclude __pycache__/ \
  /path/to/BE_Analyze/backend/ /opt/electricity-app/
sudo chown -R root:root /opt/electricity-app
sudo python3.12 -m venv /opt/electricity-app/.venv
sudo /opt/electricity-app/.venv/bin/pip install --upgrade pip
sudo /opt/electricity-app/.venv/bin/pip install /opt/electricity-app
```

### 3. 创建生产环境文件

```bash
sudo install -d -o root -g root -m 0750 /etc/electricity-app
sudo install -o root -g root -m 0600 /dev/null /etc/electricity-app/electricity.env
sudoedit /etc/electricity-app/electricity.env
```

将第二节中的变量写入该文件；生产数据库建议使用：

```dotenv
DATABASE_PATH=/var/lib/electricity-app/electricity.db
PUBLIC_BASE_URL=https://<domain>
POLL_MINUTES=30
```

然后确认权限：

```bash
sudo stat -c '%a %U:%G %n' /etc/electricity-app/electricity.env
# 预期：600 root:root /etc/electricity-app/electricity.env
```

### 4. 安装 systemd、初始化数据库并启用定时采集

```bash
sudo install -o root -g root -m 0644 /opt/electricity-app/deploy/electricity-app.tmpfiles.conf /etc/tmpfiles.d/electricity-app.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/electricity-app.conf

sudo install -o root -g root -m 0644 /opt/electricity-app/deploy/electricity-app.service /etc/systemd/system/electricity-app.service
sudo install -o root -g root -m 0644 /opt/electricity-app/deploy/electricity-sync.service /etc/systemd/system/electricity-sync.service
sudo install -o root -g root -m 0644 /opt/electricity-app/deploy/electricity-sync.timer /etc/systemd/system/electricity-sync.timer
sudo install -o root -g root -m 0644 /opt/electricity-app/deploy/electricity-backup.service /etc/systemd/system/electricity-backup.service
sudo install -o root -g root -m 0644 /opt/electricity-app/deploy/electricity-backup.timer /etc/systemd/system/electricity-backup.timer
sudo systemctl daemon-reload

sudo systemd-run --quiet --wait --pipe --collect --unit=electricity-init-db \
  --property=User=electricity --property=Group=electricity \
  --property=WorkingDirectory=/opt/electricity-app \
  --property=EnvironmentFile=/etc/electricity-app/electricity.env \
  /opt/electricity-app/.venv/bin/electricity-admin init-db

sudo systemctl enable --now electricity-app electricity-sync.timer electricity-backup.timer
sudo systemctl status electricity-app electricity-sync.timer --no-pager
```

`electricity-sync.timer` 使用环境变量 `POLL_MINUTES=30`，因此每 30 分钟采集一次。首次部署后可手动触发：

```bash
sudo systemctl start electricity-sync.service
sudo journalctl -u electricity-sync.service -n 100 --no-pager
```

### 5. 配置 Nginx 与 HTTPS

用模板生成站点配置，再申请证书：

```bash
export DOMAIN=<domain>
sudo envsubst '$DOMAIN' < /opt/electricity-app/deploy/nginx-electricity.conf.template \
  | sudo tee /etc/nginx/sites-available/electricity-app >/dev/null
sudo ln -sf /etc/nginx/sites-available/electricity-app /etc/nginx/sites-enabled/electricity-app
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d <domain>
```

验证公网健康检查：

```bash
curl -fsS https://<domain>/health/live
sudo ss -lntp | grep 8000
```

应用进程应只监听 `127.0.0.1:8000`；公网只由 Nginx 暴露 80/443。

## 四、接入微信测试公众号

在测试号管理页完成以下配置。域名字段均只填写域名本身，例如 `<domain>`，不要填写 `https://`、端口或路径。

1. **接口配置信息**：
   - URL：`https://<domain>/wechat/message`
   - Token：与服务器 `WECHAT_MESSAGE_TOKEN` 完全一致。
2. **网页账号 → 获取授权用户基本信息 → 修改**：填写 `<domain>`。这是 OAuth 回调域名；它与“接口配置信息 URL”是不同的配置项。
3. （可选）**JS 接口安全域名**：填写 `<domain>`，仅当 H5 后续调用微信 JS-SDK 时需要。
4. 在自定义菜单添加“电量分析”，菜单 URL 设置为：`https://<domain>/wechat/entry`。
5. 用已关注测试号的测试微信号点击菜单并同意授权。系统首次会返回 `authorization pending`，由管理员在服务器批准该 OpenID：

```bash
sudo -u electricity /opt/electricity-app/.venv/bin/electricity-admin list-pending
sudo -u electricity /opt/electricity-app/.venv/bin/electricity-admin enable-wechat <request_id>
```

随后重新打开菜单即可进入电量分析 H5 页面。

## 五、常用运维命令

```bash
# 服务与定时任务状态
sudo systemctl status electricity-app electricity-sync.timer electricity-backup.timer --no-pager

# 实时查看服务日志
sudo journalctl -u electricity-app -f

# 手动同步与查看同步日志
sudo systemctl start electricity-sync.service
sudo journalctl -u electricity-sync.service -n 100 --no-pager

# 检查公网入口
curl -fsS https://<domain>/health/live

# 备份数据库
sudo systemctl start electricity-backup.service
sudo systemctl status electricity-backup.service --no-pager
```

部署更新时，上传新的 `backend/` 代码、重新安装依赖（若 `pyproject.toml` 有变化），再执行：

```bash
sudo systemctl restart electricity-app
sudo systemctl start electricity-sync.service
sudo nginx -t && sudo systemctl reload nginx
```

## 六、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 微信报 `10003 redirect_uri 域名与后台配置不一致` | 到“网页账号 → 获取授权用户基本信息”设置 OAuth 域名；填写裸域名 `<domain>`。 |
| 微信报 `10005` 或没有 scope 权限 | 测试号使用 `snsapi_userinfo`，确认测试微信号已关注测试号且在测试用户列表；重新从菜单进入。 |
| 页面显示 `authorization pending` | 在服务器使用 `electricity-admin list-pending` 查到请求号，再执行 `enable-wechat <request_id>`。 |
| Nginx 返回 502 | 先执行 `sudo systemctl status electricity-app --no-pager` 与 `sudo journalctl -u electricity-app -n 100 --no-pager`；常见原因是环境文件变量缺失或格式错误。 |
| 采集没有新数据 | 检查 `electricity-sync.timer` 是否激活、物业账号是否有效、房间/设备名称是否与上游接口返回一致。 |

## 七、提交与密钥管理

根目录 `.gitignore` 已排除 `.env`、SQLite 数据库、虚拟环境、缓存与日志。提交前仍建议执行：

```powershell
git status --short
git diff --cached --check
```

请将生产配置保存在服务器 `/etc/electricity-app/electricity.env`，并在任何凭据发生泄露、截图外发或成员变更时立即轮换。
