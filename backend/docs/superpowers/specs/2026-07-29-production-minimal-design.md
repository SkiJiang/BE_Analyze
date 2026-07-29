# 生产极简化设计

## 目标

将项目收敛为仅由微信公众号菜单打开的电量分析 H5 服务，删除不再使用的小程序及开发过程文件，并将服务器运行形态收敛为一个 FastAPI systemd 服务。

## 保留的功能

- 物业接口的只读采集、30 分钟一次同步与每日补偿同步。
- SQLite 持久化、电量与费用分析、健康检查和管理 CLI。
- 微信消息回调、网页 OAuth 授权、OpenID 审批及 H5 仪表盘。
- Nginx HTTPS 反向代理所需的最小配置说明。
- 后端自动化测试。

应用进程已经通过 APScheduler 在启动后每 30 分钟执行同步，因此不再部署独立的同步 service 或 timer。

## 文件结构

最终仓库仅保留根 README 和 `backend/`。后端保留 Python 源码、测试、环境变量示例、一个 systemd unit 与一个静态 H5 页面资源。H5 页面由 `FileResponse` 提供，页面自身、样式与脚本仍是必要运行资源，但不再使用 Jinja2 或 `templates/` 目录。

## 删除范围

- 删除小程序工程、类型定义、微信开发者工具配置和 Node 配置文件。
- 删除 `backend/docs/superpowers/` 中仅用于本次开发过程的设计与计划记录。
- 删除 Jinja2 依赖和 `templates/` 目录；将仪表盘改为静态 HTML 响应。
- 删除 Nginx 配置模板、备份 service/timer、tmpfiles 规则和 smoke-test 脚本。
- 删除重复的后端部署 README；根 README 作为唯一部署文档。

数据库备份从自动 timer 改为运维人员按需要使用 SQLite 的 `.backup` 命令执行，避免额外 unit、timer、目录和保留策略。

## 部署结构

系统只安装 `electricity-app.service`。该 service 使用 systemd `StateDirectory=electricity-app` 管理 `/var/lib/electricity-app`，因此不需要 tmpfiles 配置。应用保留监听 `127.0.0.1:8000` 的单 worker；Nginx 是唯一对公网开放的 HTTP 服务。

根 README 直接给出一份替换 `<domain>` 后即可使用的 Nginx server block，以及上传、虚拟环境安装、环境文件、service 启动和验证命令。真实密钥仍只存放在 `/etc/electricity-app/electricity.env`。

## 验证

- 删除前后运行完整后端测试集。
- 新增或更新测试，确认 `/dashboard` 返回静态 H5 页面，且未授权用户仍跳转至微信授权入口。
- 静态检查确认没有 Jinja2、模板目录、小程序目录或已删除部署文件的引用。
- 以 `git diff --check` 和 Git 状态确认最终提交内容。
