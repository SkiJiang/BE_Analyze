# 微信测试公众号消息接口验证设计

## 目标

在不改变既有微信公众号 H5 OAuth 流程的前提下，增加微信测试公众号“接口配置”所需的服务器校验端点。该端点仅用于验证 URL 和 Token；本次不接收、存储或处理用户消息。

## 配置

新增 root-only 环境变量 `WECHAT_MESSAGE_TOKEN`，长度至少 16 个字符。它与 `WECHAT_APP_SECRET`、会话密钥及 OpenID HMAC 密钥相互独立，且不得写入日志、响应或源码。

## 路由与数据流

- 新增 `GET /wechat/message`。
- 请求必须包含 `signature`、`timestamp`、`nonce`、`echostr` 四个查询参数，均为非空字符串。
- 服务按微信约定对 `token`、`timestamp`、`nonce` 进行字典序排序、拼接、SHA-1 摘要，并使用恒定时间比较 `signature`。
- 校验成功返回原样 `echostr`，状态码 200；缺参或校验失败返回 403，且不回显任何参数。
- 新增 `POST /wechat/message`，返回空的 200 响应，用于避免测试号在尚未实现消息能力时重试。它不解析或记录请求体。

## 隐私与部署

- Uvicorn 访问日志对 `/wechat/message` 的查询字符串只保留路径，避免记录签名和 `echostr`。
- Nginx 对该路由关闭访问日志，沿用 HTTPS 与反向代理保护。
- 生产环境在 `/etc/electricity-app/electricity.env` 中生成并保存 Token，权限维持 `600 root:root`。

## 验证

- 配置测试覆盖有效签名回显、无效签名拒绝、缺参拒绝、POST 空响应及日志脱敏。
- 部署后用本地签名请求验证 200 回显，并运行完整测试、Nginx 配置检查和 HTTPS 冒烟检查。
- 微信测试公众号后台填写 URL `https://xingbao.icu/wechat/message` 与同一 Token 后提交验证。
