# 更新日志

---

## v0.3.0 - 2026-07-20

- 完成 Registry v2：managed key 使用 `(owner_platform_id, owner_user_id, endpoint_name)`，pending key 使用 `(owner_platform_id, request_id)`，实现多 Bot / 多 adapter 实例隔离。
- 增加 Registry v1 透明迁移、私有备份、quarantine、原子 candidate transaction、幂等重载与 fail-closed 校验；quarantine 仅保留 legacy Webhook 兼容投递，普通用户不可发现或管理。
- 完成 `aiocqhttp` 与 `qq_official` 双通道群验证；QQ 官方 WebSocket 普通 QQ 群的真实主动 Webhook / OMP 图片卡片 smoke 已通过。
- 增加离线 `platform_id` rebind helper 与 runbook；该能力不作为聊天命令或 Plugin Page 功能提供。
- 加固凭据交付：私聊不输出字符串 URL，Endpoint Path 与 Token 分离；Token 通过 direct send 发送且不进入 RespondStage 日志，失败时不重试、不回滚，由 owner 私聊 rotate 恢复。
- 命令帮助动态适配 `/`、`!` 与空 `wake_prefix`；异常配置使用安全占位符和诊断提示，静态文档统一使用 `<唤醒词>`。
- 增加 owner hard delete，仅允许删除当前 scope 的 `revoked` / `expired` managed endpoint；active 必须先 revoke，删除后原 path 返回 404 且旧 Token 失效。
- 完善全局超级管理员私聊命令 `list`、`revoke-path`、`revoke-owner`，使用精确选择器和脱敏审计，绝不展示 Token。
- Plugin Page 增加认证 `GET /astrbot_plugin_webhook_notifier/base-url`，只返回 `base_url` 与 `configured`，并提供 Base URL 复制入口。
- 增加 `uv.lock` 与 `pyproject.toml` 的 `dev` dependency group，用于本地锁定 PyYAML、pytest、pytest-asyncio 与 Pillow 等验证依赖；当前 GitHub Actions 发布流程仍使用 pip，尚未启用 Ruff 门禁或 `uv.lock` 强制同步。
- 补齐 GitHub Actions Release 环境的 Pillow 测试依赖，确保 HTML 图片渲染与裁剪测试在正式发布流水线中执行。

---

## v0.2.0 - 2026-07-15

- **安全默认值变更**：新增 `enable_private_notifications`，默认 `false`；Webhook 状态通知不再默认主动投递到 `FriendMessage`，群聊通知不受影响。
- 升级后现有私聊 endpoint 与 Token 保持有效，无需重建；默认返回 HTTP 200、`message=skipped`、`retryable=false`、`rendered=false`，开启配置并 reload 后恢复投递。
- 混合目标继续发送群聊，私聊目标标记为 `skipped`，整体返回 `message=partial_delivery`；只有真实发送失败才标记 `retryable=true`。
- 明确 OneBot/NapCat 与 QQ 官方 Bot 的主动消息风险边界，并新增平台投递策略文档；不提供风控对抗方案，不按平台分叉仓库。
- 新增 HTML 卡片图片渲染、图片结果校验、截图空白裁剪和纯文本降级。
- 新增 AstrBot Plugin Page 模板管理，可查看内置模板并创建、复制、编辑、预览、保存、应用和删除自定义模板。
- 使用本地 Monaco Editor 与 inline workers，支持 HTML 编辑、JSON 预览数据和 sandbox `srcdoc` 预览。
- 新增 version 1 模板 registry、不可变 revision 文件、并发 revision 检查和 active 自定义模板到内置模板的渲染回退。

---

## v0.1.0 - 2026-07-09

- 实现 Webhook Notifier 文本链路 MVP（Milestone 1）。
- 支持 endpoint 级 token 注册、验证、轮换和撤销。
- 支持 OMP `session_stop` Webhook 解析、文本渲染和 UMO 消息发送。
- 支持 `session.model` 字符串或对象格式，并保留 `round.turnId: 0`。
- 新增本地打包脚本、VSCode 打包启动配置和 GitHub Actions 自动 Release 流程。
