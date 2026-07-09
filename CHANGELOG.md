# 更新日志

---

## v0.1.0 - 2026-07-09

- 实现 Webhook Notifier 文本链路 MVP（Milestone 1）。
- 支持 endpoint 级 token 注册、验证、轮换和撤销。
- 支持 OMP `session_stop` Webhook 解析、文本渲染和 UMO 消息发送。
- 支持 `session.model` 字符串或对象格式，并保留 `round.turnId: 0`。
- 新增本地打包脚本、VSCode 打包启动配置和 GitHub Actions 自动 Release 流程。
