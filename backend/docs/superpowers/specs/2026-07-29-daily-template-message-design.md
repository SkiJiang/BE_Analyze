# 每日电量模板消息设计

## 目标

每天 20:00（Asia/Shanghai）向已批准的微信测试号用户发送“电量用量分析提醒”模板消息。消息使用测试号后台已创建的模板 ID，并链接到现有 H5 仪表盘。

## 模板数据

发送数据严格使用模板已定义的字段：`room`、`date`、`today_energy`、`today_cost`、`balance`、`week_energy`、`updated_at`、`request_url`。模板 ID 仅从 `WECHAT_DAILY_TEMPLATE_ID` 环境变量读取，不进入源码、README、日志或测试输出。`request_url` 与模板消息点击链接均为 `PUBLIC_BASE_URL + /wechat/entry`。

## 用户身份与安全

当前授权表只保存 OpenID HMAC，不能用于微信发送。新增独立 Fernet 加密密钥环境变量 `WECHAT_OPENID_ENCRYPTION_KEY`，在获批准用户完成 OAuth 回调时加密保存原始 OpenID。授权判断继续使用 HMAC；所有日志只记录结果和数量，不包含 OpenID、access token、AppSecret 或密文。

## 发送与去重

新增微信模板消息客户端：以 AppID/AppSecret 获取并缓存 access token，调用模板消息发送接口。新增按 `(date, openid_hmac)` 唯一的投递成功记录；只有微信接口返回成功后才写入记录，因此重复执行不会重复发送，失败可以再次尝试。

## 调度与手动验证

在现有 APScheduler 新增每日 `20:00` 任务。数据超过现有陈旧阈值时跳过发送。新增 `electricity-admin send-reminder`，用于在服务器上手动测试当天模板消息；首次测试前，已批准用户需要重新打开一次公众号菜单，以补全加密 OpenID。

## 验证

测试数据库迁移与加密 OpenID 存储，使用 HTTP mock 验证 token 缓存和模板消息 payload，验证同日去重、陈旧数据跳过、20:00 调度和 CLI 手动发送。完成后运行全部测试，部署到服务器，配置两项新环境变量，并通过手动命令向测试号验证。
