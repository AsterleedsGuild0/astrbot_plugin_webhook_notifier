# Webhook Notifier 安全与运维指南

本文面向 AstrBot 管理员、插件运维者和需要配置外部 Webhook 调用方的用户，说明凭据交付、Registry、平台边界、故障恢复与公开协作中的脱敏要求。

---

## 安全模型概览

Webhook Notifier 采用 Endpoint 级最小权限模型：每个 Endpoint 绑定 owner、provider 和目标白名单，外部 payload 不能指定任意目标 UMO。一个 Token 泄露时，影响范围被限制在对应 Endpoint 的目标边界内。

机机调用使用：

```http
Authorization: Bearer <TOKEN>
```

Token 不从 URL query 读取。公网暴露时应由反向代理或隧道层提供 HTTPS；插件内置 HTTP 服务默认监听本地地址，不负责公网 TLS 证书管理。

首次部署完整链路时请阅读[端到端部署教程](end-to-end-setup.md)。反向代理至少应遵循以下原则：公网只开放 HTTPS 所需入口，不直接暴露插件端口；同机部署保持插件 loopback 监听；跨机部署只绑定受控私网接口，并由防火墙限制为仅反向代理主机可访问；转发时保留完整 `base_path` 与 Endpoint Path；限制可能记录 Endpoint Path 的访问日志权限、导出范围和保留期。Caddy 只是参考实现，其他反向代理满足同等边界即可。

---

## URL、Path 与 Token 的分离交付

聊天侧实行“零字符串 URL”规则：命令回复不展示 URL scheme、host、domain、配置中的 `public_base_url`，也不输出 Webhook URL 环境变量赋值。

三类信息分开获取和保管：

- **Base URL**：仅通过已认证的 AstrBot Plugin Page 获取。
- **Endpoint Path**：在创建、确认、列表或状态相关的安全聊天摘要中展示。
- **Bearer Token**：仅在 private create、rotate 或 QQ 官方 confirm 成功时独立发送一次。

Plugin Page 的 Base URL 接口只返回 Base URL 与是否已配置，不返回 Token、Registry、owner、Endpoint、目标 UMO 或服务端密钥。配置了 `public_base_url` 时，该值被视为已经包含所需基础路径语义；页面不会再次自动拼接 `base_path`。

### Token direct send 边界

创建、轮换或 QQ 官方确认成功时：

1. 插件先产生不含 Token 的正常安全摘要。
2. 随后通过当前事件的 `event.send()` 直接发送恰好一个关闭 T2I 与 Markdown 的 Plain，内容仅为 `Bearer Token: <TOKEN>`。
3. Token 不进入 `MessageEventResult` 或 RespondStage。
4. direct send 期间临时日志过滤器只匹配当前 Token 的精确值，并在发送结束后移除。

插件只承诺对 adapter 发起一次 direct send，不承诺网络 exactly-once。若发送抛出异常或送达状态不确定：

- 不自动重试；
- 不回滚已经提交的 create、confirm 或 rotate；
- 不恢复旧 Token；
- 用户应在同一平台私聊执行 `token rotate <名称>` 生成新凭据。

普通命令、异常和兼容文本若意外包含插件 Token 格式，会被替换为 `[Token 已隐藏]`。

---

## Bearer 鉴权与 Token 持久化

Token 由插件使用安全随机数生成。Registry 持久化只保存基于本地 `server_secret` 的 HMAC-SHA256 hash 与算法标识，不保存 Token 明文；鉴权时重新计算 hash，并使用恒定时间比较。

`server_secret` 与 Registry 数据同属关键恢复材料：只备份其中一个可能导致恢复后的 Token 无法继续验证。两者都不得进入公开仓库、Issue、聊天或普通日志。

### 旧 Token 失效语义

- `rotate` 成功提交后，旧 Token 立即失效，新 Token 成为唯一有效凭据。
- 即使新 Token 的 direct send 失败，旧 Token 也不会恢复；再次 `rotate` 才能恢复可控凭据。
- `revoke` 后 Endpoint 停止鉴权，但记录保留。
- `delete` 后记录被永久移除，原 Path 返回 404，旧 Token 永久无效。
- 终态记录删除后可以同名重建；即使确定性 Path 被复用，新 Token 也不会恢复旧 Token 权限。

---

## 私聊主动通知安全默认值

配置项：

```yaml
enable_private_notifications: false
```

默认关闭 Webhook 对 `FriendMessage` 目标的主动状态通知。此开关不影响：

- 聊天命令回复；
- private Endpoint 创建；
- Token 验证与交付；
- rotate、revoke、delete；
- QQ 官方 confirm；
- 群聊通知。

### 响应语义

- 全部目标都是私聊且开关关闭：HTTP 200，`message=skipped`、`delivered=false`、`rendered=false`、`retryable=false`。
- 群聊与私聊混合且开关关闭：群聊继续发送，私聊标记为 `skipped`，整体 `message=partial_delivery`。
- 只有实际调用发送 API 后发生真实发送失败，才会把相关结果标记为失败并设置 `retryable=true`。

`skipped` 是已按安全策略完成处理，不应触发外部调用方重试。OneBot/NapCat 主动私聊存在账号风控风险；QQ 官方 Bot 的主动消息也受官方规则、额度和授权范围约束。开启前请阅读并遵循[平台投递策略](platform-delivery-policy.md)。

---

## 客户端社区 Hook 与数据外发

OMP 原生提供 extension/Hook 加载机制和 `session_stop` 生命周期事件，但本插件当前兼容的 HTTP POST、环境变量和 version 1 payload 来自外部 [`ParticleG/omp-config` 社区 Hook](https://github.com/ParticleG/omp-config/blob/main/agent/hooks/post/onebot.ts)，不是 OMP 内建 Webhook。部署步骤见[OMP 客户端社区 Hook 集成指南](client-integration.md)。

客户端主要环境变量：

| 主变量 | fallback | 安全用途 |
| --- | --- | --- |
| `OMP_SESSION_WEBHOOK_URL` | `ONEBOT_WEBHOOK_URL` | 完整 Endpoint URL；应只在受控环境中组合和保存 |
| `OMP_SESSION_WEBHOOK_TOKEN` | `ONEBOT_WEBHOOK_TOKEN` | 独立 Bearer Token；不得写入 URL |
| `OMP_SESSION_WEBHOOK_TIMEOUT_MS` | `ONEBOT_WEBHOOK_TIMEOUT_MS` | 请求超时毫秒值；默认约 5 秒 |

该 Hook 可能外发 prompt、`cwd`、session file、会话/轮次标识、模型、耗时、图片与 entry/message 计数、lastAssistant 元数据。使用方必须评估业务数据、用户输入和本地路径离开 OMP 运行环境后的传输、接收、日志与留存风险。

当前上游实现失败时记录 warn、不重试，因此临时网络故障可能造成单次通知丢失；timeout 过小会增加此风险。失败日志可能记录 URL，但不应记录 Token 或完整 payload，仍应避免把敏感信息放入 URL，并限制日志访问。

外部仓库独立维护且未见明确 License。本项目只链接、不复制或分发其源代码；使用、固定版本或再分发前，应自行确认上游许可和兼容性。生产环境建议记录实际采用的上游 commit，并在升级 OMP 或 `omp-config` 后重新验证 payload 与日志行为。

---

## OpenCode V1 Plugin 与数据边界

OpenCode 接入使用仓库内的 `integrations/opencode/webhook-notifier.ts`，API 基线为 `v1.17.9`；当前实际本机 CLI/Desktop 目标为 `1.18.4`。配置必须使用 OpenCode V1 的二元 `plugin` tuple：`[模块 URL, options]`，模块默认导出为 `{id, server}`。完整配置、CLI smoke 和 Desktop 安全边界见 [OpenCode 集成指南](opencode-integration.md)。

AstrBot Endpoint 创建时可显式选择：

```text
<唤醒词>whn token new private <名称> --provider opencode
```

省略 `--provider` 时默认 `omp`；provider 在 Endpoint 创建后不可变。OpenCode Plugin 只使用 Endpoint 的 Base URL、Endpoint Path 和 Bearer Token 组合请求，不读取或修改 AstrBot auth/secrets。

### OpenCode 发送白名单

Plugin 只发送 `opencode.session_idle`、`opencode.session_error`、`opencode.permission_asked` 和 `opencode.question_asked` 四类 V1 envelope。原始 session ID 使用带上下文前缀的 SHA-256 截断值作为匿名 `session.ref`；名称会移除危险 Unicode、控制字符并限制长度。title 使用会话名，缺失时使用 `OpenCode Session <匿名 ref>`；`source.name` 按 `instanceDisplayName` > `OpenCode` 选择。项目路径只按 `worktree`、`project.worktree`、`directory` 优先级取最后一级作为 `projectName`，不发送完整路径；卡片详细字段始终包含 `sessionName`，有项目名时增加“项目”行。会话 `time.created` 只形成 `startedAt`；当前/最后 Assistant 的 `info.time.created/completed` 形成可选的 `taskStartedAt`/`endedAt`，只有两者都可靠时才有 task duration；`session.time.updated` 不参与其中。

Question/Permission 的 `actionContentMode` 默认为 `strict`，只保留类别和计数；`summary` 只发送清洗、截断摘要；`full` 才发送显式 allowlist 中完整（仍受上限约束）的问题文本、选项 label/description/推荐信息、权限标题/描述/操作目标或 patterns。`full` 必须由调用方明确 opt-in，因为它可能把业务文本和目标路径外发。它与服务端 `notification_mode` 正交，不能混用解释。

服务端 `notification_mode` 只有 `focused` 和 `all`：`focused` 只抑制成功完成的 `subagent` 与 `auxiliary`，root 的 completed/failed/action_required、辅助或 subagent 的 failed/action_required、unknown scope 以及未知未来状态全部放行；`all` 保持全部通知。默认 auxiliary 只精确匹配清洗后的 `smartfetch-secondary`，非空 `parentID` 始终优先为 `subagent`。unknown fail-open。被过滤事件返回 HTTP 200 `message=skipped`，并明确返回 `scope` 与 `reason`，不调用 renderer、HTML/T2I 或 sender。

客户端还支持安全、一次性的运行时元数据诊断：`metadataDiagnostics` 默认 `off`，可设为 `once` 或 `sample`，非法值回退为 `off`。`once` 每阶段每进程最多一条；`sample` 每阶段最多 8 条，对完全相同的安全 payload 去重，超过上限静默停止。每条有 session ID 的 sample 诊断可带进程内递增 `sampleSession`，同一匿名 session 跨阶段复用，不输出 raw ID、匿名 ref/hash 或 message/parent ID。诊断只记录经过安全键名过滤的 bounded key、短字符串、响应形状、状态枚举、长度和存在性，Assistant 时间只记录 `created/completed` 的存在性或 key 名；不记录标题/名称正文、parent ID 值、消息正文、parts、路径、Token、URL、headers、response body 或异常 message。

思考深度取证只允许 Assistant `info` 中的 `variant`、`reasoningEffort`、`reasoning_effort`，以及 `session.get` 的 nested model 和顶层对应候选。string 会清洗并限长，number/boolean 原样限界记录，对象/数组只记录类型；`reasoning` 正文、parts、provider options 和未知对象值始终不展开。nested model 的键名最多 24 个，候选字段在日志中明确区分 `model*` 与 `topLevel*`，不会加入 outgoing envelope。

Client 复用已有 `input.client.session.get()` 判断 scope：非空字符串 `parentID` 为 `subagent`；只有 root/unknown 且名称精确匹配 `smartfetch-secondary` 或受限自定义 auxiliary 名称时为 `auxiliary`，明确 `undefined/null` 为 `root`，API 失败、非对象、空字符串或类型异常为 `unknown`。`session.get` 只提供 `time.created` 作为会话开始时间；`session.time.updated` 不用于任务结束或耗时。OpenCode v1.18.4 的 `message.updated.properties.info` 仅在 `role=assistant` 时被消费；清洗后的 `mode` 作为 agent，`providerID/modelID` 作为 model 元数据，`info.time.created/completed` 作为任务时间，并按匿名 session ref 放入最多 1000 条、触发后保留最近 500 条的 LRU。若 agent/model 或 Assistant 时间仍缺失，Client 最多调用一次 `session.messages(limit=10)`，逆序读取最后 assistant 的 `info`，不读取 `parts`。`parentID` 只在 Client 内部参与判断；若诊断开启也只记录 `parentIDState`，不记录其值。缓存也不保存 raw ID、message ID、parts、路径、tokens 或 cost。只缓存可靠 enum 和时间值，并对状态/缓存做有界清理。部署顺序必须是服务端先升级并重载，OpenCode Client 后完全重启，因为旧服务端严格 allowlist 不接受新 `session.scope`。

以下数据不得进入 OpenCode 请求、诊断日志或服务端 `raw`：

- 原始 session ID、cwd、完整本地路径、prompt、消息、tool、command 和 diff；
- `parentID` 值；它只在 Client 内部参与 scope 判断，诊断最多记录 `parentIDState`，不能进入请求、服务端存储或渲染；
- strict 模式下 permission 标题、描述、目标路径，以及 question 正文、选项正文；full 模式也不得通过未列入 allowlist 的键绕过边界；
- error message、response body、Token、URL 和未列入 allowlist 的字段。

服务端对未知字段 fail-closed。不要绕过官方 Plugin 直接发送 OpenCode 原始 event object，也不要为了显示调试信息而放宽字段白名单。`session.get` 与 `session.messages` 的异常只产生固定脱敏 warning；不记录异常文本、session/ref、标题、响应正文或其他请求内容，且失败不阻断通知。需要排查运行时结构时，选择明确档位（例如低/高复杂度的新会话）运行多个新会话，只在受控环境临时开启 `metadataDiagnostics=once` 或 `sample`，完成取证后恢复 `off`；诊断输出不得复制到 payload、缓存、Issue 或长期运维记录。

### OpenCode retry 与恢复

单次请求默认 timeout 为 10 秒，最多 3 次尝试。network error、timeout、429 和 5xx 可重试；401、403、413 及其他 4xx 不重试。该链路是 at-least-once 风格的尽力投递，不是 exactly-once；超时发生在服务端已接收之后可能造成重复，调用方应按稳定 `id` 去重。

Token 失效或 Endpoint 配置错误时不要在 URL 中嵌入新 Token。回到同一平台的 AstrBot 私聊执行 `token rotate <名称>`，并安全更新 OpenCode 的 env/file 配置。OpenCode Plugin 的配置缺少 URL/Token 时会安全禁用，不应通过降低鉴权或扩大白名单恢复。

---

## Registry v2 的用户可见概念

Registry v2 按平台实例隔离用户资产，核心 scope 为：

- managed Endpoint：`owner_platform_id + owner_user_id + endpoint_name`；
- pending request：`owner_platform_id + request_id`。

这意味着同一用户在不同 Bot 实例或不同 `platform_id` 下的同名 Endpoint 是不同记录。普通管理命令不会跨平台搜索或猜测归属。

### managed

正常创建并由当前平台 scope 管理的 Endpoint。用户可以按状态执行 list、rotate、revoke 或对终态记录执行 delete。

### pending

群聊验证中的临时申请。`aiocqhttp` 验证成功后会清理 pending 并进入 tokenless active；`qq_official` 群验证后 pending 转为 `group_verified_waiting_owner`，直到原申请者私聊 confirm 才清理。

pending 有不可延长的有效期。缺失、无时区或不可解析的 expiry 会 fail-closed：关联 Endpoint 过期并清理 pending，而不是继续接受验证。

### quarantine

从旧版 Registry 迁移时，无法安全确定平台归属的 legacy 记录会进入 quarantine。它只为旧 Path/Token 保留兼容投递：

- 普通用户不能 list、rotate、revoke、delete、认领或按 owner/name 查找；
- 管理员列表也不展示 quarantine；
- 全局超级管理员可用精确 `revoke-path` 作为 kill switch 关闭投递。

---

## v1 透明迁移与持久化保证

首次加载无版本号的 v1 Registry 时，插件会透明迁移到 canonical v2：

- 可唯一推断平台归属的记录进入 managed；
- 无法安全归属的记录进入 quarantine；
- v1 pending 不继续作为可验证凭据，相关待验证记录按安全规则终止；
- 原文件先写入私有备份，再发布 v2。

迁移与后续 Registry 写操作遵循：

- **原子**：候选快照完整校验后，通过临时文件、文件同步与原子替换提交；
- **幂等**：canonical v2 重复加载不会重复迁移或覆盖已有迁移备份；
- **fail-closed**：格式、版本、不变量、备份或持久化失败时拒绝发布部分状态；
- **内存/磁盘一致**：写入失败时不发布候选内存快照。

Registry 加载失败不是“忽略坏数据继续服务”的场景。应保持服务关闭或不可用，保留现场并从受控备份恢复，不要手工删字段后直接重启试错。

---

## owner 与管理员操作

### owner 操作

- `rotate`：当前 scope 内轮换 Token，旧 Token 立即失效。
- `revoke`：软撤销 Endpoint，保留终态记录与审计信息。
- `delete`：仅永久删除当前 scope 内 `revoked` / `expired` managed Endpoint。

`active` 必须先 revoke；`pending_verification` 不能强制 hard delete；quarantine 不可通过 owner 命令管理。

### 管理员操作

仅 AstrBot 全局超级管理员可在私聊执行：

- `admin token list`：最多展示 50 条 managed 最小元数据；
- `admin token revoke-path`：按完整 Endpoint Path 精确撤销，并可关闭 quarantine；
- `admin token revoke-owner`：按 `platform_id + owner_user_id + 名称` 精确撤销 managed Endpoint。

所有选择器均为精确匹配，不提供模糊搜索或跨平台推断。聊天输出和审计日志不得包含 Token 明文、完整 hash、验证码或完整目标 UMO。管理员在工单中也应使用不可逆指纹或占位符代替真实选择器值。

---

## 多 Bot 平台隔离与目标边界

`platform_id` 表示具体 adapter 实例的管理边界，不只是 adapter 类型名称。多 Bot 环境中：

- managed、pending、owner 管理和 QQ 官方 confirm 都按 `platform_id` 隔离；
- 其他 Bot 实例中的同名 Endpoint 对当前实例表现为不存在；
- 管理员 `revoke-owner` 必须显式给出平台、owner 与名称，不能只按名称处理；
- target UMO 的平台前缀必须与记录所属 `platform_id` 一致。

目标 UMO 是 Endpoint 白名单边界。外部 payload 不能提交任意 UMO，只能在 Endpoint 已绑定的目标别名范围内选择，因此 Token 不能被用来向任意用户或群发送消息。

---

## platform_id 变化与离线 Rebind

adapter 实例重建或迁移后，`platform_id` 可能变化。不要通过聊天命令、直接编辑 JSON、替换字符串或按 adapter 名称猜测归属。

项目提供独立 helper 迁移 Registry v2 managed 记录。完整步骤见 [platform_id 离线 Rebind Runbook](platform-id-rebind-runbook.md)。关键边界：

- dry-run 为零写入只读操作，但仍建议停服以获得稳定快照；
- execute 与 rollback 必须先停止 AstrBot 和插件；
- 必须显式提供 `--confirm-offline`；
- rebind 保持 Endpoint Path 与 Token hash，不迁移 quarantine；
- execute/rollback 会永久清空全部 pending，并使待验证记录过期；
- 使用 durable backup、manifest 和 digest guard 控制提交与回滚。

未确认完全停服时，不得执行 rebind 写操作。

---

## 故障恢复清单

### Token 未收到

1. 不要要求 Bot 重发旧明文 Token，也不要从日志或 Registry 中寻找明文。
2. 确认创建、confirm 或 rotate 的安全摘要是否已经成功。
3. 在创建 Endpoint 的同一平台私聊执行 `token rotate <名称>`。
4. 立即更新外部系统；上一个 Token 已失效或送达状态不可确认。

### Base URL 未配置或不知道在哪里获取

1. 在已认证的 AstrBot Dashboard 中打开插件 Plugin Page。
2. 从页面复制 Base URL；聊天命令不会显示完整 URL。
3. 若 `public_base_url` 为空，页面会返回由监听配置构成的本地 Base URL；这不代表外部网络可以访问。
4. 需要公网访问时，在部署层配置 HTTPS 反向代理或受控隧道，并把权威 Base URL 配置为包含所需基础路径的值。

### HTTP 服务未运行

1. 使用状态命令确认插件启用状态、HTTP 服务状态和可投递 Endpoint 数量。
2. HTTP 服务仅在存在可投递 Endpoint 时自动启动；pending 或 tokenless Endpoint 不等于可鉴权 Endpoint。
3. 检查监听地址/端口冲突、配置格式、Registry 初始化和启动错误摘要。
4. 不要在 Issue 中粘贴真实监听公网地址、完整请求头或 Endpoint Path。

### HTML/T2I 失败

1. 确认 `render_mode` 与 `fallback_to_text`。
2. 检查 AstrBot `html_render` / T2I 服务是否可用、是否超时、是否返回有效 PNG/JPEG/WebP。
3. active 自定义模板失败时，插件会先尝试内置模板；仍失败时按配置降级为纯文本。
4. 若纯文本可以送达，优先保持服务运行并单独修复模板/T2I，不要通过泄露 payload 的方式排障。

### Registry 加载失败

1. 停止 AstrBot 与插件，阻止新的 Registry 写入。
2. 保留原文件、私有迁移备份和相关错误摘要；不要公开内容。
3. 核对文件版本、JSON 完整性、权限、磁盘空间和最近升级/恢复操作。
4. 从同一时点的受控 Registry 与 `server_secret` 备份恢复，并先在隔离副本验证可加载性。
5. 不要绕过版本或不变量校验，不要把 quarantine 手工改为 managed。

### platform_id 变化

1. 停止创建、轮换和验证操作。
2. 确认旧、新 `platform_id` 与迁移范围，不按 adapter 名称猜测。
3. 按 rebind runbook 先 dry-run，再在停服且带 `--confirm-offline` 的条件下 execute。
4. 验证新 scope 的 list、rotate、revoke 与受控鉴权 smoke，再恢复服务。

---

## 支持边界

当前支持承诺仅覆盖：

- `aiocqhttp`：既有命令、普通群通知和 HTML 图片卡片；主动私聊不建议开启。
- `qq_official` WebSocket 私聊：命令、鉴权和受平台规则约束的私聊通知。
- `qq_official` WebSocket 普通 QQ 群：双阶段群验证和主动 Webhook/OMP 通知。
- OpenCode：通过 V1 Plugin 发送上述四类 envelope；服务端仍受 Endpoint provider、Bearer 鉴权和目标白名单约束。

明确不支持且暂无支持计划：

- QQ 频道（Guild）；
- `qq_official_webhook` 接入方式。

`qq_official` 的 WebSocket 接入方式仍使用标准 adapter key `qq_official`，不要自行使用不存在的 adapter key。已完成的 smoke test 不代表平台允许无限主动发送，仍须遵守官方规则、额度和 Bot 授权范围。

---

## 备份与升级建议

### 备份

- 在停服或确保无并发写入的维护窗口备份插件数据目录中的 Registry、`server_secret`、模板 Registry 与自定义模板文件。
- 备份目录应限制访问权限，并与公开日志、构建产物和源码仓库分离。
- Registry 与 `server_secret` 应作为同一恢复单元保存；恢复后先做离线加载验证。
- 保留 v1 迁移私有备份、rebind manifest 和自动 backup 时，不要修改其内容；按组织保留策略加密归档或安全销毁。

### 升级

1. 升级前记录当前插件版本、配置开关和脱敏状态摘要。
2. 在维护窗口创建受控备份。
3. 阅读版本说明，特别关注 Registry、平台验证和默认安全开关变化。
4. 升级后检查 Registry 加载、Endpoint 数量、HTTP 服务、模板与最小鉴权 smoke。
5. `enable_private_notifications` 默认关闭不会删除现有私聊 Endpoint 或使 Token 失效；确认风险后开启并 reload 即可恢复投递。

---

## 日志与 Issue 脱敏清单

提交日志、截图、Issue、工单或聊天记录前，删除或替换：

- Bearer Token、Authorization 头和 Token hash；
- `server_secret` 及其文件内容；
- 完整 Base URL、域名、公网 IP、反向代理地址和 Webhook URL 环境变量赋值；
- Endpoint Path；
- `platform_id`、owner/user 身份、群号、群 openid、member openid；
- 完整 target UMO、会话标识和消息来源标识；
- request_id、验证码和 pending 原始内容；
- Registry、备份、manifest 的完整内容；
- 原始 Webhook payload、prompt、工作目录、本地文件路径和可能包含业务数据的模板预览；
- 任何可关联真实 Bot、账号、组织或部署环境的截图元素。

推荐只提供：插件版本、AstrBot 版本、adapter 类型、错误类型、HTTP 状态码、`message` / `error` 枚举、是否可重试、经过检查的最小日志摘要，以及使用 `<PLACEHOLDER>` 替换后的复现步骤。

---

## 相关文档

- [端到端部署教程](end-to-end-setup.md)
- [命令参考](command-reference.md)
- [OMP 客户端社区 Hook 集成指南](client-integration.md)
- [OpenCode 集成指南](opencode-integration.md)
- [平台投递策略](platform-delivery-policy.md)
- [platform_id 离线 Rebind Runbook](platform-id-rebind-runbook.md)
