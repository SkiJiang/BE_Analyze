# 微信测试公众号消息接口验证 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让微信测试公众号能够通过 HTTPS URL 与独立 Token 验证本服务，同时保持现有 H5 OAuth 和数据采集行为不变。

**Architecture:** 在既有 FastAPI router 中新增独立的 `/wechat/message` GET/POST 路由。配置层新增一个 root-only Token；GET 按微信 SHA-1 规则校验后回显 `echostr`，POST 不解析消息内容且返回空 200。Uvicorn 与 Nginx 均不记录该路由的查询参数或正文。

**Tech Stack:** Python 3.12、FastAPI、Pydantic Settings、pytest、Nginx、systemd。

## Global Constraints

- 仅服务当前 7号楼/8F/805 电表账户，不新增任何物业写入能力。
- `WECHAT_MESSAGE_TOKEN` 至少 16 字符，只能存在于 root-only 环境文件。
- Token、signature、timestamp、nonce、echostr 和消息正文不得写入应用日志、Nginx 日志或响应错误正文。
- `GET /wechat/message` 仅在四个非空参数齐全且 SHA-1 签名一致时返回原样 `echostr`；其他情况返回 403。
- `POST /wechat/message` 返回空 200，不解析、不持久化消息正文。
- 不改变 `/wechat/entry`、`/wechat/callback`、物业同步、H5 API 或 30 分钟调度行为。
- 当前目录不是 Git 仓库；不执行提交。

---

### Task 1: 配置与签名验证路由

**Files:**
- Modify: `src/electricity_app/config.py`
- Modify: `src/electricity_app/web.py`
- Modify: `.env.example`
- Test: `tests/test_config.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `Settings.wechat_message_token: SecretStr`。
- Produces: `GET /wechat/message?signature=str&timestamp=str&nonce=str&echostr=str -> Response` 与 `POST /wechat/message -> Response`。
- Produces: `wechat_message_signature(token: str, timestamp: str, nonce: str) -> str`，供路由与测试生成一致签名。

- [ ] **Step 1: 写入失败测试**

在 `tests/test_config.py` 的基础环境字典中添加 `wechat_message_token`，并增加缺失 Token 时 Settings 验证失败的断言。于 `tests/test_web.py` 增加：

```python
def test_wechat_message_verification_echoes_echostr_for_valid_signature(client, settings):
    query = {
        "timestamp": "1710000000",
        "nonce": "nonce-value",
        "echostr": "wechat-challenge",
    }
    query["signature"] = wechat_message_signature(
        settings.wechat_message_token.get_secret_value(),
        query["timestamp"],
        query["nonce"],
    )
    response = client.get("/wechat/message", params=query)
    assert response.status_code == 200
    assert response.text == "wechat-challenge"


def test_wechat_message_verification_rejects_invalid_or_missing_signature(client):
    assert client.get("/wechat/message", params={
        "signature": "wrong", "timestamp": "1", "nonce": "n", "echostr": "e"
    }).status_code == 403
    assert client.get("/wechat/message").status_code == 403


def test_wechat_message_post_returns_empty_success(client):
    response = client.post("/wechat/message", content=b"<xml>private</xml>")
    assert response.status_code == 200
    assert response.content == b""
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py tests/test_web.py -k "message" -q`

Expected: FAIL，因为 `wechat_message_token`、签名函数和 `/wechat/message` 路由尚不存在。

- [ ] **Step 3: 编写最小实现**

在 `Settings` 中加入：

```python
wechat_message_token: SecretStr = Field(min_length=16)
```

在 `.env.example` 中加入空的 `WECHAT_MESSAGE_TOKEN=`。在 `web.py` 定义：

```python
def wechat_message_signature(token: str, timestamp: str, nonce: str) -> str:
    material = "".join(sorted((token, timestamp, nonce))).encode("utf-8")
    return hashlib.sha1(material).hexdigest()
```

在 `create_router` 中定义 GET 路由：缺少任意参数时 `HTTPException(status_code=403)`；使用 `hmac.compare_digest` 比较计算结果与请求签名；成功时 `Response(content=echostr, media_type="text/plain")`。定义 POST 路由并返回 `Response(status_code=200)`，不读取 `Request.body()`。

- [ ] **Step 4: 运行定向测试并确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py tests/test_web.py -k "message" -q`

Expected: PASS。

- [ ] **Step 5: 提交**

不适用：当前目录不是 Git 仓库。

### Task 2: 访问日志与 Nginx 隐私保护

**Files:**
- Modify: `src/electricity_app/web.py`
- Modify: `deploy/nginx-electricity.conf.template`
- Test: `tests/test_web.py`
- Test: `tests/test_deployment.py`

**Interfaces:**
- Consumes: `/wechat/message` 路由。
- Produces: Uvicorn 访问日志仅记录 `/wechat/message` 路径；Nginx 的该 location 关闭 access log。

- [ ] **Step 1: 写入失败测试**

在 `tests/test_web.py` 为 `_UvicornCallbackQueryRedactionFilter` 添加路由测试：

```python
def test_uvicorn_access_log_redacts_message_query_values():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "%s", (), None
    )
    record.args = ("127.0.0.1", "GET", "/wechat/message?signature=private&echostr=challenge", "1.1", 200)
    _UvicornCallbackQueryRedactionFilter().filter(record)
    assert record.args[2] == "/wechat/message"
```

在 `tests/test_deployment.py` 新增：

```python
message = _location_block(rendered, "location = /wechat/message")
assert "access_log off;" in message
assert "proxy_pass http://127.0.0.1:8000;" in message
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web.py tests/test_deployment.py -k "message" -q`

Expected: FAIL，因为日志过滤器和 Nginx location 尚未覆盖该路由。

- [ ] **Step 3: 编写最小实现**

将日志过滤器的路径条件扩展为 `/wechat/callback` 或 `/wechat/message`。在 HTTPS server 中新增精确 Nginx location：

```nginx
location = /wechat/message {
    access_log off;
    limit_req zone=electricity_oauth burst=10 nodelay;
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Host $host;
}
```

- [ ] **Step 4: 运行定向测试并确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web.py tests/test_deployment.py -k "message" -q`

Expected: PASS。

- [ ] **Step 5: 提交**

不适用：当前目录不是 Git 仓库。

### Task 3: 服务器部署与验收

**Files:**
- Modify: `/etc/electricity-app/electricity.env`（服务器，root-only）
- Modify: `/opt/electricity-app/src/electricity_app/config.py`（服务器部署副本）
- Modify: `/opt/electricity-app/src/electricity_app/web.py`（服务器部署副本）
- Modify: `/opt/electricity-app/deploy/nginx-electricity.conf.template`（服务器部署副本）

**Interfaces:**
- Consumes: 通过本地测试的 Task 1、Task 2 文件。
- Produces: 供微信测试号填写的 `https://xingbao.icu/wechat/message` 与服务器内独立随机 Token。

- [ ] **Step 1: 生成 Token 并写入 root-only 配置**

在服务器使用 `openssl rand -hex 24` 生成 Token，将其写入 `/etc/electricity-app/electricity.env` 的 `WECHAT_MESSAGE_TOKEN=` 行，保持 `600 root:root`，不打印 Token。

- [ ] **Step 2: 上传并安装代码**

上传修改的源码、部署模板和测试文件到 `/opt/electricity-app`，运行：

```bash
/opt/electricity-app/.venv/bin/pip install --no-deps /opt/electricity-app
systemctl restart electricity-app
nginx -t
systemctl reload nginx
```

- [ ] **Step 3: 验证微信格式签名**

在服务器内部读取 Token 但不输出，以固定 timestamp、nonce 计算 SHA-1，向 `https://xingbao.icu/wechat/message` 发起请求。验证 HTTP 200、响应等于本地 `echostr`，错误签名返回 403。

- [ ] **Step 4: 完整回归与运维检查**

Run: `.venv/Scripts/python.exe -m pytest -q`

Run: `systemctl is-active electricity-app nginx`; `PUBLIC_HOST=xingbao.icu bash /opt/electricity-app/deploy/smoke-test.sh`; `ss -lntp | grep ':8000'`。

Expected: 本地测试全部通过，两个服务 active，HTTPS 冒烟通过，Uvicorn 只监听 `127.0.0.1:8000`。

- [ ] **Step 5: 提交**

不适用：当前目录不是 Git 仓库。
