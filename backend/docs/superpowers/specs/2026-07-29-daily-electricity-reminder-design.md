# 每日用电提醒设计

## 目标

在微信测试号中使用模板消息，每天 20:00（Asia/Shanghai）向已授权用户推送当日用电量提醒。消息点击后打开现有电量分析 H5 页面。

## 消息方式

使用测试号模板消息接口，而不是接收普通消息或客服消息。接收普通消息只处理用户主动发给公众号的内容；客服消息受互动时间窗口限制，不能用于稳定的每日提醒。

环境变量 `WECHAT_DAILY_TEMPLATE_ID` 保存测试模板 ID。模板变量固定为 `room`、`date`、`today_energy`、`today_cost`、`balance`、`week_energy`、`updated_at` 和 `remark`。发送链接使用 `PUBLIC_BASE_URL + /dashboard`，未登录用户会由已有路由转入网页 OAuth。

## OpenID 与访问控制

微信发送接口需要原始 OpenID，但现有白名单仅保存 HMAC 摘要。保留摘要作为授权判断依据，新增使用独立密钥加密的 OpenID 密文列：

- 新环境变量 `WECHAT_OPENID_ENCRYPTION_KEY` 使用 Fernet 格式的密钥。
- OAuth 回调在用户已获授权时写入或更新该密文。
- 已有的授权记录无法从摘要恢复 OpenID；用户重新打开一次公众号菜单即可补全密文。
- 不向日志、CLI 输出或 HTTP 响应暴露原始 OpenID、密钥、access token 或 AppSecret。

## 发送流程

新增独立的 `WeChatTemplateClient`：

1. 使用 AppID 与 AppSecret 获取并在进程内缓存 access token，缓存到过期前一分钟。
2. 读取加密 OpenID 的已授权用户，解密后调用模板消息发送接口。
3. 使用 `AnalyticsService.dashboard()` 生成当天的用电、费用、余额和近七日汇总。
4. 每个用户当天发送成功后写入投递记录；失败不写成功记录。

数据库新增投递表，以 `(day, openid_hmac)` 作为唯一键。任务重启、手动触发或重复执行不会对同一用户重复推送成功消息。

## 调度与故障处理

在现有 APScheduler 中新增 `daily_reminder` 任务，每天 20:00 运行。任务在当日数据陈旧时跳过发送并记录不含敏感信息的警告；消息 API 失败也不标记成功。运维人员可使用 CLI 手动触发当天提醒，用于测试和补发。

## 验证

- 使用 HTTP mock 测试 access token 缓存、模板消息请求结构、接口失败与敏感信息不进入日志。
- 测试 OAuth 回调仅为已授权用户保存加密 OpenID，且密文不能直接读取为原始 OpenID。
- 测试 20:00 调度定义、同日去重、陈旧数据跳过及 CLI 手动触发。
- 运行完整测试集，并在服务器配置模板 ID 和加密密钥后执行一次手动测试发送。
