# 更新日志

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
