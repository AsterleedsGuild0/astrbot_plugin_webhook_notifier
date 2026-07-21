# AstrBot Webhook Notifier FSD

## 文档信息

- 文档类型：FSD（Software Functional Specification Document）
- 文档版本：v1.0.0
- 对应 PRD 版本：v1.0.0
- 对应插件版本：v1.0.0
- 状态：Final / MVP 已归档 / 1.x 稳定契约
- 最后更新：2026-07-21
- 项目名称：`astrbot_plugin_webhook_notifier`
- 产品名称：Webhook Notifier
- 目标仓库：`AsterleedsGuild0/astrbot_plugin_webhook_notifier`

---

## 目的与范围

本文档定义 Webhook Notifier `v1.0.0` 的 Final 功能规格，作为 1.x 后续实现、测试和验收的稳定功能契约。MVP 已归档；正式 Git tag、GitHub Release 与 ZIP 尚待创建。云端已验证保留数据目录/配置数据的卸载 v0.3.0 后重装 RC 数据兼容性，但未验证原位升级、在线更新或 AstrBot 插件市场一键更新路径，后者属于正式版发布/上架后的检查项。

本文档描述：

- 插件功能模块与边界。
- HTTP Webhook 接口行为。
- 鉴权、事件识别和错误响应。
- `ParticleG/omp-config` 社区 post Hook 的 OMP `session_stop` version 1 payload 适配规则。
- 标准化事件对象结构。
- 目标会话路由和消息发送规则。
- Webhook 私聊通知的安全默认值、投递 preflight 与 Sender 兜底职责。
- 文本渲染、HTML 卡片渲染和降级策略。
- Plugin Page 模板管理、模板 registry、preview 与并发控制。
- 配置项、状态命令、安全与可观测性要求。

本文档不定义底层具体实现细节，例如具体使用哪个 HTTP 框架、文件拆分路径或内部类名；这些内容应在后续技术设计文档中确定。

---

## 系统边界

### 插件内部职责

Webhook Notifier 负责：

- 提供 Webhook HTTP 入口。
- 校验请求身份。
- 解析并识别外部事件。
- 将 provider 专用 payload 转换为标准化事件对象。
- 按配置选择目标会话。
- 渲染通知内容。
- 调用 AstrBot 消息发送能力投递通知。
- 记录安全、精简、可排障的运行日志。

### 插件外部依赖

Webhook Notifier 依赖：

- AstrBot 插件加载机制。
- AstrBot 配置系统。
- AstrBot 消息发送能力和 UMO 目标格式。
- AstrBot `html_render` / T2I 能力，用于 HTML 卡片图片模式。
- AstrBot `aiocqhttp` 与 `qq_official` 消息平台，用于 QQ 群聊、私聊和图片消息投递。
- OneBot 验证环境使用 NapCat；QQ 官方 WebSocket 私聊与普通 QQ 群均已完成对应能力验证。
- OMP 原生 extension/Hook 加载机制与 `session_stop` 生命周期事件，以及独立维护的 `ParticleG/omp-config` 社区 post Hook；HTTP POST、环境变量和 version 1 payload 由该社区 Hook 提供，不是 OMP 内建 Webhook。

MVP 支持范围：

- 声明支持 `aiocqhttp` 与 `qq_official` 的已验证能力边界；QQ 官方普通群真实主动 Webhook / OMP HTML 图片卡片 smoke 已通过，但仍受官方主动消息规则、额度与 Bot 授权范围约束。
- QQ 频道（Guild）与 `qq_official_webhook` 明确不在插件支持范围内，也没有支持计划；不得从 `qq_official` 私聊或普通群验证结果外推兼容性。
- `aiocqhttp` 验证环境使用 NapCat，群主/群管理员识别依赖 `await event.get_group()` 的权威群资料。
- `qq_official` 群批准只读取当前群 raw `group_openid`、`author.member_openid` 与 `author.member_role`，不调用不存在的成员查询 API。
- 其他 AstrBot 平台适配器未测试，不在当前支持承诺内。

术语说明：

- 文档中的 QQ 群聊和 QQ 私聊按段落明确区分 `aiocqhttp` 与 `qq_official`，不得把一个 adapter 的身份或状态机契约外推到另一个 adapter。
- target UMO 使用 pending 所属 `platform_id` 构造；示例可使用 `aiocqhttp:GroupMessage:<group_id>` 或 `qq_official:GroupMessage:<group_openid>`。

### 插件不负责

Webhook Notifier 不负责：

- 管理底层 QQ 登录或 OneBot 连接。
- 适配 `aiocqhttp`、`qq_official` 以外的 AstrBot 消息平台。
- 替代 GitHub/GitLab 的完整 App 集成。
- 提供开放公网网关服务的统一认证平台。
- 管理外部系统的任务生命周期。
- 保证 QQ 平台图片上传一定成功。
- 在线协调独立 Registry 运维进程；`platform_id` rebind execute/rollback 仅由独立 helper 在 AstrBot 与插件停止后离线执行，不提供聊天命令或 Plugin Page UI。

Registry v2 提供离线 `platform_id` rebind helper。dry-run 只读且零写入；execute/rollback 要求显式停服确认，原子保持 path 与 Token hash，更新 managed scope 和 target UMO 前缀，永久废弃全部 pending，并使用 digest guard、durable backup 与脱敏 audit 支持受限 rollback。运维步骤见 `docs/platform-id-rebind-runbook.md`。

---

## 功能模块

### Webhook HTTP Server

提供 HTTP POST 入口，接收外部系统事件。

MVP 支持多个 endpoint。每个 endpoint 对应独立 path、provider、token 哈希、申请者和目标白名单。

```text
POST /webhook/omp-session
```

MVP 通用路径：

```text
POST /webhook/u/{owner_hash}/{endpoint_name}
```

`endpoint_name` 在同一个申请用户内唯一；`owner_hash` 用于隔离不同申请用户的同名 endpoint。

功能要求：

- 只接受 POST。
- 只接受 JSON body。
- 限制请求体大小。
- 在 token 未配置时不得启动公网可用 Webhook 服务。
- 默认监听 `127.0.0.1`，公网暴露由反向代理、隧道或用户部署层处理。
- 插件内置 HTTP Server 不直接管理 TLS 证书；公网访问必须通过 HTTPS，由 Nginx、Caddy、Cloudflare Tunnel 或其他部署层组件完成 TLS 终止。
- 不支持也不建议直接把插件 HTTP 端口裸露到公网。
- 处理完成后返回明确 JSON 响应。

### Auth

负责校验请求身份。

MVP 只支持 Bearer Token：

```http
Authorization: Bearer <token>
```

功能要求：

- token 由插件生成，并绑定 endpoint、申请者和目标白名单。
- token 明文格式为 `whn_` 前缀加 32 字节随机值的 URL-safe base64 字符串，由 `secrets.token_urlsafe(32)` 生成。
- 持久化时保存 token 哈希，不保存明文 token。
- token 哈希算法使用 `HMAC-SHA256(server_secret, token)`，其中 `server_secret` 为插件首次初始化时生成并保存在插件数据目录中的本地密钥；日志和状态命令不得展示该密钥。
- token 校验使用 constant-time compare。
- token 明文只在 private create、rotate 或 QQ 官方 confirm 成功时通过私聊展示一次。
- endpoint 无 token 哈希时不应处理外部请求。
- 不支持 URL query token。
- 不在日志中打印 token 原文。

MVP 不支持单全局 token 作为 Webhook 调用凭据。骨架阶段的 `webhook_token` 配置仅作为早期占位，MVP 实现时应迁移为 endpoint 级 token registry。

### Token Model Decision

MVP 采用 endpoint/token 绑定目标白名单模型：

```text
endpoint path + bearer token → endpoint registry record → allowed targets
```

该模型的功能要求：

- 每个 endpoint 独立 token。
- 每个 token 绑定 owner user id。
- 每个 token 绑定 provider。
- 每个 token 绑定 target whitelist。
- token 可按 endpoint 独立撤销和轮换。
- 请求审计至少能关联 endpoint、owner、provider 和目标。

不采用单全局 token 的原因：

- 单 token 泄露会影响全局。
- 单 token 无法区分调用者身份。
- 单 token 轮换会影响所有用户。
- 若单 token 搭配 payload 指定目标，容易产生任意投递风险。
- 若单 token 再叠加目标白名单，本质上会重新演化为 endpoint/token 模型。

### Simple Mode Evaluation

simple mode 指单人单目标的简化配置，例如一个全局 token 和一组默认目标。

评估结论：

- simple mode 不进入 MVP。
- simple mode 适合单人自用、单目标、内网部署或快速试用。
- simple mode 会引入第二套鉴权和路由语义，增加 MVP 复杂度和安全文档负担。
- 后续如需要支持，应通过独立 issue 规划，并默认关闭。

后续 simple mode 必须满足：

- 明确标注仅建议单人/内网使用。
- 默认不允许 payload 指定任意 UMO。
- 如果支持多个目标，也必须使用 target alias 白名单。
- 支持从 simple mode 迁移到 managed endpoint mode。

### Token Provisioning

负责用户自助申请、验证、发放、轮换和撤销 Webhook Token。

MVP 支持两类申请：

- 私聊目标 token。
- 群聊目标 token。

私聊目标 token：

- 用户必须在私聊中申请。
- token 只能绑定申请者与 Bot 的私聊 UMO。
- 创建成功后先 yield 安全摘要；async generator 恢复后通过 `event.send()` 单次 direct send 仅含 Token 的 Plain。敏感消息不得进入 `MessageEventResult`。

群聊目标 token：

- 用户在私聊中发起申请；aiocqhttp 预指定数字群号，QQ 官方使用 `current` 延迟绑定当前验证群。
- 插件创建待验证申请和一次性验证码。
- aiocqhttp 由原申请者到预指定群发送验证命令，插件确认其是该群 owner/admin。
- QQ 官方可由目标群任一 owner/admin 发送验证命令，不要求批准者是 C2C 申请者，也不比较 `member_openid` 与 private owner。
- aiocqhttp 验证通过后不生成 token，提示原申请者私聊 rotate；QQ 官方群管理员 verify 后只转 waiting-owner，提示原 C2C 申请者同平台私聊 confirm。

群聊验证成功后的凭据约束：

- 不调用 `Context.send_message()` 主动私聊发送凭据，避免平台主动私聊限制与凭据误投递。
- aiocqhttp endpoint 进入 tokenless `active`，在 rotate 前不接受鉴权；QQ 官方在 confirm 前保持 `pending_verification`。
- aiocqhttp verify 成功后清理 pending；QQ 官方 verify 只清 challenge 并转 `group_verified_waiting_owner`，confirm 成功才删除 pending。验证码只以 HMAC hash 持久化，不保存 pending code 或 token 明文。
- QQ 官方 confirm 成功时生成并独立交付一次 Token；若 Token 消息发送失败，不回滚 active 状态，原 owner 通过同平台私聊 rotate 恢复。

待验证申请格式：

- `request_id` 使用 UUID4 字符串。
- `code` 使用 `secrets.token_hex(3)` 生成 6 位小写十六进制验证码。
- 默认有效期为 10 分钟。
- `request_id + code` 只能使用一次；验证成功、过期或取消后均应失效。
- 验证失败不展示正确验证码。

Token 生命周期：

- 创建：private endpoint 生成 token 明文并保存哈希，以独立 Plain 消息返回明文；group verify 不生成 token。
- QQ 官方确认：原 C2C owner 在同 platform private confirm 后生成 token、保存哈希、激活 endpoint、删除 pending，并以独立 Plain 消息返回明文。
- 轮换：生成新 token 并替换 token hash，旧 token 立即失效，无宽限期。
- 撤销：使用软删除，设置 `revoked_at` 并保留记录用于审计。
- 永久删除：`<唤醒词>whn token delete <名称>` 仅允许 owner 在私聊删除同 platform + owner + name scope 的 `revoked` / `expired` managed record；操作不可恢复。`active` 必须先 revoke，`pending_verification` 只能完成、过期或 revoke，不提供强制删除；quarantine 不可通过 owner/name 发现或删除。
- 同名重建：终态记录永久删除后不再占用 `owner_platform_id + owner_user_id + endpoint_name` 命名空间及 deterministic path；同名重建可复用 path，但必须生成新 Token，旧 Token 保持无效。
- 列表：普通 `<唤醒词>whn token list` 默认只展示 active 和 pending_verification endpoint 的 name、path、provider、target aliases、created_at，不展示 token 明文和完整 token hash；revoked/expired 记录保留在持久化数据中用于审计，但不在默认列表中展示。
- 已撤销 endpoint 的请求返回 403 `endpoint_revoked`。
- 已永久删除 endpoint 的请求返回 404 `not_found`。

群管理员识别基于目标群的群消息事件，并按 adapter 使用其权威群角色数据；AstrBot `event.is_admin()` 仅表示 AstrBot 管理员身份，不参与群管理员判定。

群管理员校验按 adapter 明确实现。`aiocqhttp` 必须 `await event.get_group()` 后比对群主与管理员，AstrBot super-admin 不构成群管理员捷径。`qq_official` 只读取群消息 `raw_message.raw_data` 的 `group_openid`、`author.member_openid` 与 `author.member_role`，不读取 `user_openid`，不比较群 member 与 private owner，也不调用成员 REST API。

如果上述权威数据无法取得，群聊 token 验证必须失败，并提示“当前平台无法校验群管理员身份”。该场景不激活 endpoint，不返回 token。

#### Token Provisioning 状态机

Endpoint / token 申请记录使用以下状态：

| 状态 | 含义 | 可进入方式 | 可退出到 |
| --- | --- | --- | --- |
| `pending_verification` | 群聊申请尚未完成可用凭据流程；QQ 官方群 verify 后仍保持该状态 | 私聊按平台执行 `new group` | `active`、`expired`、`revoked` |
| `active` | endpoint 已激活；aiocqhttp 群 endpoint 可处于 tokenless active | private create、aiocqhttp verify 或 QQ 官方 confirm | `revoked` |
| `expired` | 待验证申请过期 | 超过验证有效期 | 终态 |
| `revoked` | endpoint 已撤销 | 用户撤销、管理员撤销或安全策略撤销 | 终态 |

状态规则：

- 私聊 token 不进入 `pending_verification`，创建成功后直接进入 `active`。
- 群聊 token 必须先进入 `pending_verification`；aiocqhttp verify 后进入 tokenless `active`，QQ 官方 verify 后仍 pending，直到 private confirm 才进入 `active`。
- `pending_verification` 记录不得接受 Webhook 请求。
- `expired` 和 `revoked` 记录不得接受 Webhook 请求。
- `expired` 和 `revoked` 记录不得阻止同 owner 使用相同 endpoint name 和 path 重新创建 endpoint。
- `rotate` 只允许作用于 `active` endpoint，轮换后仍保持 `active`，旧 token 立即失效。
- `revoke` 可作用于 `pending_verification` 或 `active` 记录。

QQ 官方 `bind_current_group` pending 内部 phase：

- `awaiting_group_admin`：`verified_group_id=null` 且 `code_hash` 非空，等待群 owner/admin verify。
- `group_verified_waiting_owner`：`verified_group_id` 非空且 `code_hash=""`，record 仍为 `pending_verification`，等待原 C2C owner 私聊 confirm。
- 两个 phase 共用创建时的同一个 `expires_at`，任何转换不得延长或刷新。
- waiting-owner 过期后删除 pending，并将关联 record 标记为 `expired`、清除 pending 字段。

#### 私聊目标申请序列

```text
User -> Bot(private): <唤醒词>whn token new private [name]
Bot -> Bot: 校验命令来自私聊
Bot -> Endpoint Registry: create endpoint(status=active, target=user_private)
Endpoint Registry -> Bot: endpoint path + token 明文
Bot -> User(private): 返回安全摘要 Plain
Bot -> User(private): event.send 单次发送仅含 Bearer Token 的 Plain
```

私聊创建成功返回契约：

- endpoint name。
- 第一条包含 endpoint name、`Endpoint Path` 和“Base URL：请在 Plugin Page 中复制”，不包含 Token 明文或完整 URL。
- 第二条只包含 `Bearer Token: <token>`，不得包含 Path、说明或环境变量。
- 摘要为正常 Plain result；Token chain 恰好一个 Plain，并设置 `.use_t2i(False)`、`.use_markdown(False)`。
- Token direct send 期间为 root handler 及 AstrBot/botpy/aiocqhttp logger/handler 临时安装当前 Token 精确值 filter，finally 移除。

#### 群聊目标申请序列

```text
User -> Bot(private): aiocqhttp 使用 group <数字群号>；qq_official 使用 group current
Bot -> Bot: 校验命令来自私聊
Bot -> Endpoint Registry: create pending verification(request_id, code, expires_at)
Bot -> User(private): 返回 request_id、code、过期时间和群内 verify 命令
User -> Bot(group): <唤醒词>whn token verify <request_id> <code>
Bot -> Bot: 校验 request_id/code、phase 与不可延长 expiry
Bot -> adapter event: aiocqhttp 校验预绑定群、原申请者与群资料；qq_official 校验 raw 当前群任一 owner/admin
Bot -> Endpoint Registry: aiocqhttp 直接 tokenless active；qq_official 转 group_verified_waiting_owner
Bot -> Group: aiocqhttp 提示 rotate；qq_official 提示原申请者私聊 confirm
User -> Bot(private): qq_official <唤醒词>whn token confirm <request_id>
Bot -> Endpoint Registry: 校验原 private owner 后 active、写 target/token hash、删除 pending
```

群聊验证失败分支：

- `request_id` 不存在、已过期或已使用：群聊验证聊天命令失败，并返回面向用户的安全提示；这不是 Webhook POST HTTP 响应。
- `code` 不匹配：验证失败，不展示正确 code。
- aiocqhttp 执行者不是原申请者：验证失败，不激活 endpoint；QQ 官方不要求群批准者是 C2C 申请者。
- `prebound_group` 当前群不是申请目标群：验证失败，不激活 endpoint。
- `bind_current_group` 在验证成功时绑定 QQ 官方 raw `group_openid`。
- 执行者不是群主或群管理员：群聊验证聊天命令失败，并返回面向用户的安全提示；这不是 Webhook POST HTTP 响应。
- 平台无法提供群权限信息：验证失败，提示当前平台无法校验群管理员身份。

命令参数说明：

- `aiocqhttp` 的 `<数字群号>` 是用户可见的目标 QQ 群号，对应 `group_binding_mode=prebound_group`。
- `qq_official` 使用 `current`，对应 `group_binding_mode=bind_current_group` 且申请时 `target_group_id=null`。
- pending phase 从 `awaiting_group_admin` 转为 `group_verified_waiting_owner`；`expires_at` 始终沿用创建值，不刷新。
- target UMO 始终由 Registry 使用 pending 的 `platform_id` 与验证后的 group id 构造，命令层不得传入任意 UMO。

#### Token 管理序列

查看 endpoint：

```text
User -> Bot(private): <唤醒词>whn token list
Bot -> Endpoint Registry: list endpoints by owner_user_id
Bot -> User(private): 返回 endpoint 摘要，不展示 token 明文和完整 hash
```

轮换 token：

```text
User -> Bot(private): <唤醒词>whn token rotate <endpoint_name>
Bot -> Endpoint Registry: 校验 owner_user_id 与 endpoint 状态
Bot -> Endpoint Registry: 生成新 token，替换 token hash
Bot -> User(private): 返回轮换摘要 Plain
Bot -> User(private): event.send 单次发送仅含新 Bearer Token 的 Plain
```

撤销 endpoint：

```text
User -> Bot(private): <唤醒词>whn token revoke <endpoint_name>
Bot -> Endpoint Registry: 校验 owner_user_id
Bot -> Endpoint Registry: 设置 revoked_at 与 status=revoked
Bot -> User(private): 返回撤销成功摘要
```

永久删除终态 endpoint：

```text
User -> Bot(private): <唤醒词>whn token delete <endpoint_name>
Bot -> Endpoint Registry: 在单个 candidate transaction 内按 platform + owner + name 校验终态、清理同 platform 关联 pending、删除 managed record 并持久化
Bot -> User(private): 仅返回名称、永久删除确认与不可恢复警告
```

持久化失败时 candidate 不发布，内存与磁盘保持原状。并发 delete/delete 至多一次成功；delete 与 create/rotate/revoke 由 Registry `RLock` 线性化，reload 后必须与最终内存快照一致。删除审计日志只记录 operation、result、status、platform 等安全摘要，不记录 owner 原值、path、Token/hash、UMO 或 pending code。

### Endpoint Registry

负责保存 endpoint/token/target 绑定关系。

Endpoint Registry 是用户通过 Bot 命令创建出来的运行时安全状态存储。它必须持久化到插件数据目录，例如 `data/webhook_tokens.json`。AstrBot 重启后，已创建且未撤销的 endpoint/token 绑定关系必须继续有效。

Endpoint Registry 的职责不是展示配置，而是作为 Webhook 鉴权和路由的唯一事实来源。用户创建私聊或群聊 token 后，插件必须把 owner、endpoint path、token hash、target whitelist 和状态写入 registry。

Registry v2 的主键与隔离边界固定为：

- managed key：`(owner_platform_id, owner_user_id, endpoint_name)`。
- pending key：`(owner_platform_id, request_id)`。
- 所有普通 owner 查询、轮换、撤销、永久删除和 QQ 官方 confirm 都不得跨 `owner_platform_id` 推断记录。

v1 加载时执行透明、幂等、fail-closed 迁移：可唯一推断平台的 legacy record 进入 managed 区；无法安全推断归属的记录进入 quarantine；v1 pending 不延续为可验证凭据。发布迁移结果前必须先创建私有 v1 backup，再以 candidate transaction、临时文件、file `fsync`、原子 replace 与目录 `fsync` 提交。任何校验、备份或提交前失败都不得发布部分内存状态；重复加载 canonical v2 不重复迁移或覆盖备份。

quarantine 仅兼容既有 Path/Token 的 Webhook 投递，不参与普通用户 list、rotate、revoke、delete，也不能按 owner/name 认领。超级管理员可使用精确 `revoke-path` 作为 kill switch；管理员列表不得展示 quarantine 或任何 Token。

管理员 Registry 命令仅允许 AstrBot 全局超级管理员在私聊执行：`list` 返回最多 50 条 managed 最小元数据；`revoke-path` 按完整 Endpoint Path 精确匹配；`revoke-owner` 按 `owner_platform_id + owner_user_id + endpoint_name` 精确匹配。命令、返回与审计日志必须脱敏，不展示 Token、完整 hash、验证码或完整目标 UMO，且不得使用模糊选择器或跨平台推断。

配置文件中的 `endpoints` 仅作为可读示例或管理视图，不作为 MVP 的运行时事实来源。MVP 不要求也不建议在用户通过命令创建、轮换、撤销 endpoint 后回写 AstrBot 插件配置。用户查看 endpoint 应使用 `<唤醒词>whn token list`，而不是读取配置文件。

每条记录至少包含：

- endpoint name。
- path。
- provider。
- token hash。
- token hash algorithm。
- owner user id。
- target whitelist。
- created at。
- revoked at。

注意：endpoint 记录中的 `render_mode` 和 `template` 字段已移除。旧 registry JSON 中的残留值被自然忽略，重新保存后不再保留。渲染模式读取插件全局配置，HTML 模板读取独立 Template Registry 的全局 active 模板，不提供 endpoint 级模板选择。

持久化绑定要求：

- 私聊 token 创建后，registry 记录必须绑定申请者 `owner_user_id` 和申请者私聊 UMO。
- aiocqhttp verify 或 QQ 官方 private confirm 完成后，registry 记录必须绑定申请者 `owner_platform_id`、`owner_user_id`、验证后的目标群和由 Registry 生成的群聊 UMO；QQ 官方群 verify 阶段仍保持 pending record。
- token 明文不得持久化；registry 只保存 token hash 和 hash algorithm。
- endpoint `status=active` 且 `revoked_at=null` 时才可接受 Webhook 请求。

### Template Registry 与 Plugin Page

Template Registry 是 HTML 模板的运行时唯一事实来源，与 Endpoint Registry 相互独立。管理员通过 AstrBot 插件详情页中的 Plugin Page 管理模板，不需要手工维护模板文件或通过聊天命令 reload。

存储位置位于插件数据目录：

```text
templates.json
templates/<template_id>-<revision>.html
```

`templates.json` 当前 schema version 为 `1`：

```json
{
  "version": 1,
  "active": "custom-8f6d...",
  "templates": {
    "custom-8f6d...": {
      "display_name": "OMP 卡片",
      "file": "custom-8f6d...-3.html",
      "canvas_width": 812,
      "revision": 3,
      "updated_at": "2026-07-15T12:00:00+00:00"
    }
  }
}
```

存储与缓存要求：

- `built-in` 是虚拟、只读模板，不写入 `templates` map，revision 固定为 `0`，默认画布宽度为 `812`。
- 自定义模板 ID 由后端生成，格式以 `custom-` 开头；前端不能指定文件名或路径。
- 每次保存生成新的不可变 revision 文件 `<id>-<revision>.html`，registry 只引用当前 revision。
- registry 写入使用同目录临时文件、文件 `fsync`、`os.replace` 和尽力目录 `fsync` 完成原子提交。
- 内存中同时维护不可变 registry snapshot 与模板内容 snapshot；渲染读取 active 模板时，content、revision 和 `canvas_width` 来自同一快照。
- 保存当前 active 模板会生成新 revision，并在 registry 提交成功后立即替换内存快照；active ID 不变，后续请求立即使用新内容。
- registry 声明的 active 模板缺失或无效时，`effective_active` 回退为 `built-in`，同时保留 `requested_active` 便于诊断。
- 未知 registry version 进入只读模式，禁止保存、应用和删除。
- 数据目录、模板目录和 `templates.json` 不允许是 symlink；revision 文件名、父目录和实际路径必须通过路径逃逸检查。

Plugin Page 已实现：模板列表、内置模板只读、新建、复制、删除、保存、应用、保存并应用、dirty 离开确认、HTML Monaco 编辑、JSON Monaco 预览数据和 sandbox `srcdoc` 预览。

### Template Bridge API 契约

页面通过 `window.AstrBotPluginPage` bridge 使用以下相对 endpoint；这些接口用于 Plugin Page 内部协作，不是管理员日常操作入口：

| 方法 | 相对 endpoint | 请求要点 | 成功结果要点 |
| --- | --- | --- | --- |
| GET | `base-url` | 无 | 仅 `{base_url, configured}`；不返回 Token、Registry、endpoint、owner、UMO 或 server secret |
| GET | `templates` | 无 | 模板摘要、`active`、`requested_active`、`effective_active`、只读状态、示例事件和限制 |
| GET | `templates/<id>` | URL 编码模板 ID | 完整模板内容、revision、宽度、built-in/active/valid 状态 |
| POST | `templates/save` | `id`、`display_name`、`content`、`canvas_width`、`expected_revision`、`apply` | 保存后的模板及 active 状态 |
| POST | `templates/apply` | `id`、`expected_revision` | active 与 effective active |
| POST | `templates/delete` | `id`、`expected_revision` | 删除结果及 active 状态 |
| POST | `templates/preview` | `content` 或 `id`、`event`、`canvas_width` | 注入 CSP 的 HTML 与实际画布宽度 |

保存和应用语义：

- 新建时 `id` 为空且 `expected_revision` 必须为空；后端生成 ID，初始 revision 为 `1`。
- 更新、应用和删除自定义模板时，`expected_revision` 必须等于当前 revision，否则返回 HTTP 409。
- `apply=false` 只保存，不改变 active ID；若保存对象本来就是 active，其新内容仍立即生效。
- `apply=true` 在同一次 registry 原子提交中保存并切换 active。
- 单独 apply 不创建 revision，只切换全局 active ID。
- built-in 只读但可应用，`expected_revision` 允许为空或 `0`。
- active 模板不能删除；删除先提交 registry，再尽力清理不再引用的 revision 文件。

preview 安全语义：

- preview 不写入模板文件或 registry，不改变 active。
- preview event 必须是 JSON object；canonical JSON 最大 64 KiB，深度、节点数、容器长度和字符串长度均有限制。
- event key 命中 token、password、secret、authorization、cookie、apikey 或 accesstoken 等敏感标记时拒绝。
- 模板使用 Jinja2 sandbox 与 autoescape，只注入 `event` 根变量，并允许模板内部使用 `namespace` helper。
- 拒绝 `script`、`iframe`、`object`、`embed`、`base`、`form`、事件属性、`javascript:`、`meta refresh`、手工 CSP、危险 CSS 和外部资源。
- 渲染结果最大 2 MiB，并注入 `default-src 'none'; style-src 'unsafe-inline'; img-src data:` CSP。
- 前端仅把返回 HTML 写入无权限 iframe 的 `srcdoc`，iframe 使用空 `sandbox` 和 `referrerpolicy="no-referrer"`。

### Provider Adapter

负责处理不同外部系统的专用 payload。

MVP 仅要求实现 `omp` provider。该名称是服务端兼容标识，当前具体适配 `ParticleG/omp-config` 的 `agent/hooks/post/onebot.ts` 社区 Hook 输出的 version 1 payload，不表示 OMP 原生定义了此 HTTP Webhook 契约。

后续 provider：

- `opencode`
- `github`
- `gitlab`
- `...`
- `custom`

每个 provider 输出统一的标准化事件对象。

### Normalized Event

标准化事件对象是 renderer、router 和 sender 的统一输入。

功能要求：

- provider adapter 必须输出该对象。
- renderer 不应强依赖 provider 原始 payload。
- raw payload 仅作为高级模板或调试用途。

### Router

负责根据配置选择目标会话和模板。

MVP 可只支持默认目标。

后续支持：

- 按 provider 匹配。
- 按 event 匹配。
- 按 endpoint 匹配。
- 按 payload 字段匹配。
- 多目标推送。

### Renderer

负责把标准化事件对象渲染为消息内容。

MVP 支持：

- `text`
- `html_image`

HTML 图片模式必须具备文本降级能力。

### Sender

负责把渲染结果发送到 AstrBot 会话。

功能要求：

- 支持 UMO 目标。
- 发送纯文本消息。
- 发送图片消息。
- 发送失败时返回逐目标结构化结果，由 Webhook handler 聚合为 HTTP 200 `partial_failure`；Sender 不定义独立 HTTP 500 发送错误。
- 在实际渲染和发送前执行目标投递 preflight，按 UMO 消息类型筛选可投递目标。
- 在最终发送入口再次执行同一策略作为兜底，防止调用方绕过 preflight 直接向被禁用的 `FriendMessage` 目标发送 Webhook 状态通知。

#### 私聊通知投递策略

`enable_private_notifications` 仅控制 Webhook 状态通知的 `FriendMessage` 目标，默认值为 `false`。该配置不得影响：

- 聊天命令回复。
- Token 创建、轮换后的私聊发送。
- Token 验证流程。
- endpoint 创建、激活、轮换或撤销。
- `GroupMessage` 群聊通知。

职责划分：

- Webhook handler / server 在渲染前执行 preflight，得到 `sendable_targets` 与 `skipped_targets`。若全部目标均被策略跳过，不调用 renderer，也不调用 Sender，并返回 `rendered=false`。
- Sender 在每个实际发送入口执行最终兜底检查。即使上层遗漏 preflight，也必须把禁用的 `FriendMessage` 目标返回为 `skipped`，不得调用 AstrBot 发送 API。
- Renderer 不负责判断平台或目标策略，只为至少一个可投递目标生成内容。
- Endpoint Registry 不因该配置发生任何写入、迁移或 schema 变化；现有 endpoint、Token 与目标白名单保持有效。
- 当前策略只能依据 UMO 中的 `platform_id` 与 `FriendMessage` / `GroupMessage` 判断目标类型，不能可靠区分 NapCat 与其他 OneBot 实现。

### Status Command

提供插件运行状态查看命令。

MVP 命令：

```text
<唤醒词>webhook_notifier
<唤醒词>whn
```

---

## HTTP 接口规范

### 请求方法

```http
POST
```

其他方法应返回 405 或由框架默认处理。

### 请求路径

MVP 推荐：

```text
/webhook/omp-session
```

如果实现通用 endpoint，则路径为：

```text
/webhook/u/{owner_hash}/{endpoint_name}
```

其中 `endpoint_name` 用于匹配申请者名下的 endpoint 名称；`owner_hash` 是由 `owner_user_id` 计算得到的稳定短 hash，用于 URL 命名空间隔离，不直接暴露 QQ 号或平台用户 ID。

命名规则：

- `endpoint_name` 在同一个 `owner_user_id` 下唯一。
- 不同 `owner_user_id` 可以创建相同的 `endpoint_name`。
- Registry 查询、轮换、撤销和永久删除必须按 `owner_platform_id + owner_user_id + endpoint_name` 定位记录，不能只按全局 name 查询。
- Webhook 入站请求仍以完整 path 匹配 endpoint；真正鉴权仍依赖 `Authorization: Bearer <token>`。

### 请求头

必需：

```http
Content-Type: application/json
Authorization: Bearer <token>
```

OMP 推荐：

```http
X-OMP-Event: session_stop
User-Agent: omp-session-webhook
```

### 请求体

请求体必须是 JSON object。

请求体大小限制：

- 默认上限为 256 KiB。
- 若存在 `Content-Length` 且超过上限，应在读取 body 前返回 413 `payload_too_large`。
- 若缺少 `Content-Length`，服务端读取 body 时必须累计字节数；超过上限立即停止读取并返回 413 `payload_too_large`。
- 解析 JSON 前必须先完成大小限制检查。

MVP 不接受：

- 空 body。
- JSON array 作为顶层结构。
- 表单提交。
- `text/plain`。

### 成功响应

同步处理成功：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "request_id": "b8b7b3e2-1f3a-4b7e-8d92-6b7b61c2c001",
    "provider": "omp",
    "event": "omp.session_stop",
    "delivered": true,
    "rendered": true,
    "retryable": false,
    "targets": ["default_group"],
    "render_mode": "text"
  }
}
```

投递响应的 `data` 结构至少包含：

```json
{
  "request_id": "b8b7b3e2-1f3a-4b7e-8d92-6b7b61c2c001",
  "provider": "omp",
  "event": "omp.session_stop",
  "delivered": true,
  "rendered": true,
  "retryable": false,
  "targets": ["default_group"],
  "render_mode": "text"
}
```

字段语义：

- `delivered`：至少一个目标真实发送成功时为 `true`；全 skipped 时为 `false`。
- `rendered`：本次请求是否实际执行过渲染。全部目标在 preflight 被跳过时必须为 `false`。
- `retryable`：只有实际调用发送 API 后发生真实发送失败时才为 `true`。渲染失败和策略性 `skipped` 均为 `false`。
- `targets`：本次选择到的目标名称字符串列表，即 `list[str]`；不承载逐目标状态对象。
- `send_results`：仅在存在 skipped 或 failed 结果时返回的可选逐目标详情列表；每项可包含 `name`、`ok`、`skipped`、`error`、`reason` 等发送结果字段。
- `skip_reason`：存在策略性 skipped 目标时返回的请求级稳定原因；当前值为 `private_notifications_disabled`。

全部目标被私聊安全策略跳过时返回 HTTP 200：

```json
{
  "code": 0,
  "message": "skipped",
  "data": {
    "delivered": false,
    "rendered": false,
    "retryable": false,
    "skipped": true,
    "skip_reason": "private_notifications_disabled",
    "targets": ["owner_private"],
    "send_results": [
      {
        "name": "owner_private",
        "ok": true,
        "skipped": true,
        "error": null,
        "reason": "private_notifications_disabled"
      }
    ]
  }
}
```

混合目标中，群聊成功而私聊被策略跳过时返回 HTTP 200、`message=partial_delivery`、`delivered=true`、`rendered=true`、`retryable=false`。`partial_delivery` 只表示成功投递与策略跳过并存，不表示发送错误。

若任一可投递目标在实际调用发送 API 后发生真实发送失败，响应返回 HTTP 200、`code=0`、`message=partial_failure`，保留目标名称列表和可选 `send_results`，并设置 `retryable=true`。`partial_failure` 也用于全部目标发送失败的聚合结果；名称表示批次未完全成功，不保证已有目标成功。不得因存在 skipped 目标而把策略跳过误判为失败。

```json
{
  "code": 0,
  "message": "partial_failure",
  "data": {
    "delivered": true,
    "rendered": true,
    "retryable": true,
    "targets": ["default_group", "backup_group"],
    "send_results": [
      {"name": "default_group", "ok": true, "error": null},
      {"name": "backup_group", "ok": false, "error": "session_not_found"}
    ]
  }
}
```

### 错误响应

当前 Webhook POST handler 的错误响应统一使用非 2xx HTTP 状态、`code=1`、人类可读 `message`，并在 `data` 中返回 `request_id` 与稳定 `error` 枚举：

```json
{
  "code": 1,
  "message": "Bearer Token 不匹配",
  "data": {
    "request_id": "b8b7b3e2-1f3a-4b7e-8d92-6b7b61c2c001",
    "error": "invalid_token"
  }
}
```

下表严格限定为当前 `/webhook/{endpoint}` POST 处理链真实产生的 JSON 错误。聊天命令、群验证和未来保留码不属于此表。

| HTTP 状态码 | error | 说明 |
| --- | --- | --- |
| 400 | `invalid_json` | 请求体读取失败或不是合法 JSON |
| 400 | `invalid_payload` | payload 顶层不是 object，或事件 Header/Body 结构与语义无效 |
| 400 | `unsupported_event` | OMP 事件类型或版本不支持 |
| 401 | `missing_authorization` | 缺少 Authorization |
| 401 | `invalid_token` | Authorization 不是 Bearer 格式，或 Bearer Token 不匹配 |
| 403 | `endpoint_disabled` | endpoint 不是可投递的 active 状态 |
| 403 | `endpoint_revoked` | endpoint 已撤销 |
| 403 | `token_unclaimed` | 群聊 endpoint 已验证，但创建者尚未主动私聊 rotate 领取 Token |
| 404 | `not_found` | Endpoint Path 不存在或已永久删除 |
| 413 | `payload_too_large` | 请求体超过限制 |
| 415 | `unsupported_media_type` | Content-Type 不支持 |
| 500 | `render_failed` | 文本或图片渲染失败且未能降级，或图片发送/构造失败且文本降级关闭 |
| 500 | `internal_error` | handler 发生未预期异常，或鉴权成功结果缺少 Registry record |

发送 API 的部分或全部目标失败不使用上述 500 错误响应。只要请求已完成鉴权、解析和渲染并进入逐目标发送聚合，服务端仍返回 HTTP 200、`code=0`、`message=partial_failure`：

- `delivered=true` 表示至少一个目标成功；全部发送失败时为 `false`。
- `retryable=true` 表示至少存在一个逐目标发送失败。
- `targets` 保持目标名称列表，失败细节放在 `send_results`。
- `send_results` 的失败项使用 `ok=false` 和 `error` 描述，不转换为 500。

---

## Webhook 机机交互序列

用户完成 token 申请与外部系统配置后，后续 Webhook 调用不再需要人机交互。

同步处理序列：

```text
External System(OMP)
  -> Webhook HTTP Server: POST /webhook/u/{owner_hash}/{endpoint_name}
     headers: Authorization: Bearer <token>, Content-Type: application/json
     body: OMP session_stop payload

Webhook HTTP Server
  -> Request Guard: 生成 request_id，校验 method/content-type/body size/json object

Request Guard
  -> Endpoint Registry: 按 endpoint path 查找 endpoint 记录

Endpoint Registry
  -> Auth: 校验 endpoint 状态、token hash、revoked_at

Auth
  -> Provider Adapter: 使用 endpoint.provider 解析 payload

Provider Adapter
  -> Router: 输出 Normalized Event，解析 target alias

Router
  -> Delivery Preflight: 按 enable_private_notifications 筛选 FriendMessage 目标

Delivery Preflight
  -> Renderer: 根据插件全局 render_mode 选择 text 或 html_image

Renderer
  -> Sender: 构造文本或图片消息链

Sender
  -> AstrBot OneBot v11 Message Platform: 发送到 endpoint target whitelist 内的 UMO

Webhook HTTP Server
  -> External System(OMP): 返回 JSON 处理结果，包含 request_id
```

失败短路规则：

- method、content-type、body size、JSON 解析失败时，不进入 endpoint registry。
- endpoint 不存在、未激活或已撤销时，不进入 provider adapter。
- token 校验失败时，不记录 payload 详情，不进入 provider adapter。
- provider 解析失败时，不进入 renderer/sender。
- target alias 不在白名单内时，不进入 sender。
- 全部目标在 delivery preflight 被跳过时，不进入 renderer/sender，返回 HTTP 200 `skipped`、`rendered=false`、`retryable=false`。
- 混合目标只对可投递目标渲染和发送；群聊成功、私聊 skipped 时返回 HTTP 200 `partial_delivery`。
- html_image 渲染失败且 `fallback_to_text=true` 时，继续走 text renderer 和 sender。
- sender 多目标发送时，MVP 串行执行并记录每个目标结果；部分失败时响应中必须体现失败目标。
- sender 调用 `Context.send_message()` 后必须检查返回值；返回 `False` 时不得记录为发送成功。

审计与日志关联：

- 每次 Webhook 请求必须生成 request id。
- request id 应贯穿 HTTP 响应、日志、渲染错误、发送结果和后续排障摘要。
- 日志不得记录 token 明文、Authorization header、完整 raw payload 或完整 prompt。

---

## OMP Provider 规格

本节适配对象为 `ParticleG/omp-config` 社区 post Hook 输出的 `omp.session_stop` version 1 payload。OMP 原生只提供 Hook 加载和 `session_stop` 生命周期事件；Header、HTTP POST、环境变量与本节 payload 结构属于外部社区实现。

### 事件识别

事件识别规则：

- Body `version` 的当前兼容版本为 `1`；缺失或结构变化按既有宽容字段策略处理，但未来不兼容版本不在承诺范围内。
- 仅存在 Header `X-OMP-Event: session_stop` 时，识别为 `omp.session_stop`。
- 仅存在 Body `event: omp.session_stop` 时，识别为 `omp.session_stop`。
- Header 与 Body 同时存在且语义一致时，识别为 `omp.session_stop`。
- Header 与 Body 同时存在但不一致时，拒绝请求并返回 400 `invalid_payload`。

Header `session_stop` 与 Body `omp.session_stop` 视为语义一致。

### 支持事件

MVP 仅支持：

```text
omp.session_stop
```

### 输入字段

MVP 支持读取：

```text
event
version
emittedAt
session.id
session.file
session.cwd
session.name
session.model
round.turnId
round.startedAt
round.endedAt
round.durationMs
round.prompt
round.promptLength
round.imageCount
round.entryCountBefore
round.entryCountAfter
round.entryCountDelta
round.messageCountBefore
round.messageCountAfter
round.messageCountDelta
round.stopHookActive
round.lastAssistant.provider
round.lastAssistant.model
round.lastAssistant.stopReason
round.lastAssistant.timestamp
round.lastAssistant.durationMs
metadata.version
metadata.eventName
```

`session.model` 支持两种形态：

- 字符串，例如 `"gpt-5.5"`。
- 对象，例如 `{ "provider": "openai", "id": "gpt-5.5", "name": "GPT-5.5" }`。

标准化为通知展示值时，对象形态优先使用 `provider/name`，其次使用 `provider/id`；缺少 provider 时退化为模型名。若 `session.model` 无法得到展示值，再回退到 `round.lastAssistant.model`，并可使用 `round.lastAssistant.provider` 拼接 provider。

### 字段缺失处理

- `session.name` 缺失时不使用 `session.file` basename 作为会话名兜底，避免把机器生成的 `.jsonl` 文件名暴露到默认通知；`session.file` 可保留在 `raw["session.file"]` 供高级模板显式使用。
- `session.model` 缺失时使用 `round.lastAssistant.model`。
- `round.turnId` 允许为数字 `0`，实现不得用 truthy 判断将其视为缺失；生成事件 ID 时应保留为字符串 `"0"`。
- `round.durationMs` 缺失时尝试由 `startedAt` 和 `endedAt` 计算。
- `promptLength` 缺失但 `prompt` 存在时可用字符串长度计算。
- `imageCount` 缺失时显示为 `0` 或 `未知`，实现阶段按模板策略确定。
- 非必要字段缺失不得导致插件崩溃。

### Prompt 处理

默认策略：

- 不在通知中展示完整 `round.prompt`。
- 允许配置是否包含 prompt 摘要。
- 若展示，必须按配置截断。

推荐配置：

```yaml
providers:
  omp:
    include_prompt: false
    max_prompt_length: 500
```

---

## 标准化事件对象规格

### 字段定义

```json
{
  "provider": "omp",
  "event": "omp.session_stop",
  "version": 1,
  "id": "session-id:turn-id",
  "emitted_at": "2026-07-08T12:00:00.000Z",
  "title": "会话完成",
  "status": "success",
  "summary": "",
  "source": {
    "name": "oh-my-pi",
    "url": null
  },
  "actor": {
    "name": null,
    "url": null
  },
  "fields": [],
  "links": [],
  "raw": {}
}
```

### 必需字段

- `provider`
- `event`
- `id`
- `emitted_at`
- `title`
- `status`
- `fields`

若 provider payload 缺少时间字段，`emitted_at` 使用插件接收请求时的 UTC ISO-8601 时间。

`summary` 是可选摘要。默认 OMP `session_stop` 通知可将 `summary` 置空，避免在摘要中重复展示已由 `fields` 表达的会话名和模型名。

### Raw 字段保留策略

MVP 中支持读取但未映射为 `fields` 的 OMP 字段，默认保留在 `raw` 中，不直接展示。

以下字段默认仅进入 `raw`：

- `metadata.version`
- `metadata.eventName`
- `round.stopHookActive`
- `round.entryCountBefore`
- `round.entryCountAfter`
- `round.lastAssistant.timestamp`

以下字段默认映射为通知字段：

- `session.name` → 会话；缺失时不输出会话字段。
- `session.cwd` → cwd。
- `session.model` 或 `round.lastAssistant.model` → 模型；模型对象优先展示 `provider/name`，其次展示 `provider/id`，缺少 provider 时退化为模型名。
- `round.startedAt` → 开始时间，默认格式化为本地时间加 UTC 偏移，例如 `2026-07-08 19:59:00 UTC+08:00`。
- `round.durationMs` → 耗时。
- `round.promptLength` 与 `round.imageCount` → 输入规模。
- `round.messageCountDelta` → 消息变化。
- `round.lastAssistant.stopReason` → 最后状态。

`round.endedAt` 默认不作为通知字段展示，仅在 `round.durationMs` 缺失时参与耗时计算。后续 HTML 渲染模板可以按字段自行选择展示或隐藏。

### Status 取值

MVP 支持：

```text
success
warning
failed
info
unknown
```

OMP `session_stop` 默认使用 `success`。如果后续 payload 明确包含失败状态，则映射为 `failed`。

### Field 结构

```json
{
  "label": "模型",
  "value": "openai/gpt-5.5",
  "short": true
}
```

字段要求：

- `label` 必须是短文本。
- `value` 必须可转换为字符串。
- 长值需要在 renderer 层截断或换行。

---

## 路由规格

### MVP Endpoint 路由

MVP 路由以 endpoint 为核心。每个 endpoint 绑定 provider 和 target whitelist。

配置示例：

```yaml
targets:
  - name: default_group
    umo: aiocqhttp:GroupMessage:123456789

endpoints:
  - name: alice_omp
    path: u/0a1b2c3d4e5f/alice_omp
    provider: omp
    token_hash: "..."
    owner_user_id: "10001"
    targets:
      - default_group
```

默认行为：

- 请求 path 先匹配 endpoint。
- Bearer Token 必须匹配 endpoint 的 token hash。
- provider adapter 由 endpoint 指定。
- 默认发送到 endpoint 绑定的 targets。
- 若 payload 包含 target alias，只能选择 endpoint targets 白名单内的目标。
- Registry 有效 endpoint 必须绑定目标；当前 Webhook POST 不定义配置类 HTTP 错误。进入逐目标发送后的失败统一按 HTTP 200 `partial_failure` 聚合。

安全约束：

- payload 不允许直接传入任意 UMO。
- endpoint 之外的 target alias 不可被选择。
- token 泄露时，攻击面被限制在该 endpoint 的目标白名单内。

### 后续规则路由

预留结构：

```yaml
routes:
  - name: omp_default
    match:
      provider: omp
      event: omp.session_stop
    targets:
      - default_group
    render_mode: html_image
    template: omp_session_stop.html
```

匹配顺序：

1. endpoint 精确匹配。
2. provider + event 匹配。
3. 默认路由。

MVP 可以不实现该规则路由，但配置设计需兼容未来扩展。

---

## 渲染规格

### Text Renderer

输入：标准化事件对象。

输出：纯文本字符串。

模板引擎：

- Text Renderer 与 HTML Image Renderer 均使用 Jinja2 模板语法。
- 模板渲染应使用 sandboxed environment。
- 模板上下文根变量名统一为 `event`，其值为标准化事件对象。
- 文本模板不提供顶层 `title`、`summary`、`fields` 等裸变量；必须通过 `event.title`、`event.summary`、`event.fields` 访问。

默认模板：

```text
[{{ event.source.name }}] {{ event.title }}

{% if event.summary %}
{{ event.summary }}
{% endif %}
{% for field in event.fields %}
{{ field.label }}：{{ field.value }}
{% endfor %}
```

OMP 示例：

```text
[oh-my-pi] 会话完成

会话：Add post-conversation HTTP hook
cwd：/home/user/project
模型：openai/gpt-5.5
开始时间：2026-07-08 19:59:00 UTC+08:00
耗时：57.7s
输入：977 字 / 1 张图
消息变化：+2
最后状态：stop
```

要求：

- 字段缺失时不输出 `None`。
- `summary` 为空时不输出空摘要行；默认 OMP 通知只在 fields 中展示会话名和模型名，避免重复信息。
- 长字段按配置截断。
- 不输出 token、请求头和完整 raw payload。

### HTML Image Renderer

输入：标准化事件对象和全局 active 模板快照。

输出：图片消息构造所需对象。

渲染步骤：

1. 从 Template Registry 读取 active 的不可变模板快照。
2. 将标准化事件对象传入模板。
3. 调用 AstrBot `html_render` / T2I 服务。
4. 校验渲染结果是否为图片。
5. 构造图片消息链。

自定义模板只在插件 sandbox 中执行一次 Jinja 渲染。传给 AstrBot `html_render` 的外层模板固定为 `{{ rendered_html | safe }}`，参数仅包含已经完成安全渲染的 `rendered_html`，避免 T2I 层对自定义模板进行第二次 Jinja 求值。

模板引擎：

- MVP 使用 Jinja2 模板语法，与 Text Renderer 保持一致。
- 模板渲染应使用 sandboxed environment。
- 默认模板必须自包含，不依赖外部 JS、CSS、远程字体或 CDN 图片。
- 模板上下文根变量名为 `event`，其值为标准化事件对象。
- HTML renderer 启用 autoescape；模板通过安全策略检查后才可保存或渲染。

### 模板变量

Text 与 HTML 模板均应优先使用：

```text
event.provider
event.event
event.title
event.status
event.summary
event.fields
event.links
event.source
event.actor
event.generated_at
event.event_time
```

可选高级变量：

```text
event.raw
```

模板中不提供顶层 `provider`、`title` 等裸变量；必须通过 `event.provider`、`event.title` 等命名空间访问。

### 渲染参数

默认：

```json
{
  "full_page": true,
  "type": "png",
  "quality": 90,
  "timeout": 5000,
  "viewport_width": 812,
  "viewport_height": 1200,
  "device_scale_factor_level": "high",
  "wait_until": "domcontentloaded"
}
```

`timeout` 单位为毫秒，并直接传递给 AstrBot `html_render` / T2I 服务对应截图参数。实际 `viewport_width` 始终由 active 模板的 `canvas_width` 覆盖，允许范围为 `320..2048`；模板宽度同时用于本地截图空白裁剪的缩放推算。

### 图片结果校验

渲染结果可能是：

- bytes
- URL
- `base64://...`
- 本地文件路径

当 `html_render` 返回本地文件路径时，插件会在校验后执行截图空白裁剪：

- 按默认 HTML 画布宽度裁掉右侧多余视口背景。
- 按像素差异检测裁掉底部多余背景。
- URL、base64、bytes 等非本地文件结果保持原样。
- 裁剪失败时跳过，不阻断发送和降级链路。

校验要求：

- bytes 需要检查图片 magic number。
- 本地路径需要确认文件存在、可读且是图片。
- `base64://` 需要能成功解码。
- URL 模式可交由 AstrBot 图片组件处理，但日志中不得记录敏感 query。

### 降级规则

HTML 图片模式的回退顺序：

1. 使用 active 模板完成 Jinja 渲染、T2I 截图和图片校验。
2. active 为自定义模板且任一生成阶段失败时，使用 built-in 模板重新尝试；不会重复 Jinja 渲染已生成的 HTML。
3. built-in 也失败且 `fallback_to_text=true` 时，使用 Text Renderer 生成并发送纯文本。
4. 图片消息构造或发送阶段失败时，也按配置进入纯文本降级。
5. HTTP 响应记录实际 `render_mode`、请求模式和 `fallback_reason`。

如果 `fallback_to_text` 为 false，则返回渲染失败。

---

## 发送规格

插件应通过 AstrBot 插件 API 发送消息，优先使用 `context.send_message(umo, message_chain)` 或当前 AstrBot 版本提供的等价 UMO 发送接口。

实现要求：

- 发送入口必须接受 UMO 字符串作为目标。
- 发送入口必须能发送文本消息链和图片消息链。
- 若 AstrBot API 版本差异导致方法名不同，插件应在 sender 模块内封装兼容层，FSD 其他模块不得直接依赖具体方法名。

### 目标格式

MVP 使用 UMO 字符串：

```text
platform_id:MessageType:session_id
```

示例：

```text
aiocqhttp:GroupMessage:123456789
aiocqhttp:FriendMessage:10001
```

### 文本发送

发送纯文本通知到目标会话。

要求：

- 单条文本不应超过平台可接受长度。
- 超长消息需要截断或后续拆分。

### 图片发送

发送 HTML 渲染得到的图片。

要求：

- 支持 bytes、base64、本地文件和 URL 结果。
- 图片结果无效时触发文本降级。

### 多目标发送

MVP 可以串行发送。

多目标结果应记录每个目标的发送结果。

示例：

```json
{
  "targets": ["default_group", "owner_private"],
  "send_results": [
    {"name": "default_group", "ok": true, "error": null},
    {
      "name": "owner_private",
      "ok": true,
      "skipped": true,
      "error": null,
      "reason": "private_notifications_disabled"
    }
  ]
}
```

聚合规则：

- 全部 delivered：`message=ok`、`delivered=true`、`retryable=false`。
- delivered 与 skipped 混合：`message=partial_delivery`、`delivered=true`、`retryable=false`。
- 全部 skipped：`message=skipped`、`delivered=false`、`rendered=false`、`retryable=false`。
- 存在实际调用发送 API 后的真实 failed：`message=partial_failure`，在可选 `send_results` 中逐目标保留结果并设置 `retryable=true`；是否已有其他目标成功不改变失败的可重试属性。

---

## 配置规格

### 当前配置项

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 是否启用插件 |
| `enable_private_notifications` | bool | `false` | 是否允许 Webhook 状态通知投递到 `FriendMessage`；不影响命令、Token 流程、endpoint 与群聊通知 |
| `render_mode` | string | `text` | `text` 或 `html_image` |
| `fallback_to_text` | bool | `true` | HTML 渲染失败是否降级文本 |
| `auth_mode` | string | `managed` | 预留字段；MVP 只支持 `managed`，不支持 `simple` |
| `targets` | yaml text | 示例注释 | 推送目标列表 |
| `endpoints` | yaml text | `[]` | endpoint registry 的可读配置视图；运行时以插件数据目录中的 registry 为准 |
| `render_options` | json text | 默认截图参数 | T2I 渲染参数 |

### MVP 新增建议配置

后续实现时建议补充：

```yaml
server:
  host: 127.0.0.1
  port: 18080
  base_path: /webhook
  public_base_url: https://example.com/webhook
  body_limit_bytes: 262144

storage:
  token_store: data/webhook_tokens.json
  server_secret_file: data/server_secret

targets:
  - name: alice_private
    umo: aiocqhttp:FriendMessage:10001
  - name: bot_dev_group
    umo: aiocqhttp:GroupMessage:123456789

endpoints:
  - name: alice_omp
    path: u/0a1b2c3d4e5f/alice_omp
    provider: omp
    token_hash: "..."
    owner_user_id: "10001"
    targets:
      - alice_private

providers:
  omp:
    enabled: true
    include_prompt: false
    max_prompt_length: 500
```

`public_base_url` 仅通过认证后的 Plugin Page `base-url` bridge 返回，不进入聊天消息。配置值非空时视为权威 Base URL，去除尾部斜杠且不自动追加 `base_path`；因此管理员应在配置值中包含实际所需路径。未配置时 bridge 返回由 host、port、`base_path` 组成的本地 Base URL。Base URL 已具备完整基础路径语义，页面后续只追加 `Endpoint Path`，不得再次拼接 `base_path`。

### 配置优先级（MVP 全局渲染模式）

MVP 阶段 `render_mode` 为插件全局配置，全局生效：

- 插件全局 `render_mode` 唯一决定所有 endpoint 的渲染模式。
- `EndpointRecord` 已移除 `render_mode` 和 `template` 字段。registry 加载旧 JSON 时只读取当前模型需要的字段，因此旧 registry JSON 中残留的 `render_mode`、`template` 会被忽略；重新保存后不再写出。
- endpoint 级渲染模式覆盖能力是未来独立功能，不在 MVP 范围内。即使未来重启该功能，也不会复用 `EndpointRecord.render_mode` 旧字段。
- 自定义 HTML 模板由独立 Template Registry 与 Plugin Page 管理；当前 active 模板全局生效，不提供 endpoint 级覆盖。

### 私聊通知配置迁移

- `enable_private_notifications` 是插件全局 bool，默认 `false`。
- 升级时不修改 Endpoint Registry，不撤销或重建私聊 endpoint，不轮换 Token，也不删除 `FriendMessage` 目标。
- 默认关闭时，已有私聊 endpoint 仍可通过原 URL 与 Token 完成鉴权，但 Webhook 状态通知在发送前被安全跳过。
- 管理员开启配置并 reload 插件后，现有私聊 endpoint 立即恢复投递，无需重新创建。
- 该配置只作用于 Webhook 状态通知链路；命令回复、Token 明文发送与验证必须继续工作，否则管理员将无法安全管理和恢复 endpoint。

骨架迁移：

- 若升级时检测到骨架阶段早期占位的 `webhook_token`，MVP 不自动把它作为可用 endpoint token。
- 插件应在状态命令中提示管理员使用 `<唤醒词>whn token new ...` 重新创建 managed endpoint。
- `webhook_token` 不在 MVP 配置界面展示，也不参与 Webhook 鉴权，避免误启用单全局 token 模式。

### 配置校验

启动时需要校验：

- `enable_private_notifications` 必须是 bool；缺失时按 `false` 处理。
- `render_mode` 必须是 `text` 或 `html_image`。
- `auth_mode` 在 MVP 中必须是 `managed`。
- `server.host` 默认必须是 `127.0.0.1`，除非管理员显式改为公网监听。
- `server.public_base_url` 如为空，不阻止本地服务启动；聊天只提示前往 Plugin Page，Plugin Page bridge 返回本地监听 Base URL。
- `targets` 必须能解析为列表。
- 每个 target 必须包含 `name` 和 `umo`。
- 每个 endpoint 必须包含 `name`、`path`、`provider`、`token_hash`、`owner_user_id` 和 `targets`。
- endpoint 的每个 target alias 必须存在于 `targets` 中。
- `render_options` 必须能解析为 JSON object。

---

## 状态命令规格

### `<唤醒词>webhook_notifier`

返回完整状态摘要。

内容包括：

- 插件启用状态。
- Webhook 服务运行状态。
- active endpoint 数量。
- 默认渲染模式。
- fallback 是否开启。
- 目标数量。
- 当前 active 模板及有效模板状态。
- 最近错误摘要，后续可选。

### `<唤醒词>whn`

短命令，行为与 `<唤醒词>webhook_notifier` 一致。

状态文本只由受控配置字段逐项构造，并作为专用安全结果直接输出，以保留允许展示的监听 IP、监听端口和基础路径。普通命令、用户输入和异常文本仍经过 URL/configured domain 清洗；不得因为 configured Base URL 的 netloc 与监听 host 相同而清洗状态字段。

### Token 管理命令

MVP 推荐命令形态：

本文用 `<唤醒词>` 表示 AstrBot 当前会话的 `wake_prefix`。默认 `wake_prefix=["/"]` 时命令为 `/whn ...`；改为 `!` 时使用 `!whn ...`；空前缀时使用裸命令 `whn ...`。运行时配置缺失、容器/元素类型错误、包含控制字符或读取异常时，帮助输出使用安全占位符 `<AstrBot唤醒词>` 并附诊断提示；静态示例不得用该异常占位符替代 `<唤醒词>`。

```text
<唤醒词>whn token new private [name]
<唤醒词>whn token new group <数字群号> [name]  # aiocqhttp
<唤醒词>whn token new group current [name]     # qq_official
<唤醒词>whn token verify <request_id> <code>
<唤醒词>whn token confirm <request_id>
<唤醒词>whn token list
<唤醒词>whn token revoke <endpoint_name>
<唤醒词>whn token rotate <endpoint_name>
```

私聊命令要求：

- `new private` 必须在私聊中执行。
- `new group` 必须在私聊中执行；`aiocqhttp` 只接受数字群号，`qq_official` 只接受 `current`。
- `confirm` 仅用于 QQ 官方，必须由原申请者在同 `platform_id` 私聊执行。
- `list` 只展示申请者自己的 endpoint 摘要，不展示 token 明文。
- `revoke` 和 `rotate` 只能操作申请者拥有的 endpoint。

群聊验证命令要求：

- `verify` 必须在目标群中执行。
- pending 必须先按当前 `platform_id + request_id` 命中，否则统一返回不存在或已过期。
- aiocqhttp 执行者 stable user id 必须与同平台申请者一致，并由权威群资料确认其是预指定群 owner/admin。
- QQ 官方只用当前群 raw `member_role` 确认执行者是 owner/admin；任一目标群管理均可批准，不要求与 private owner 相同。
- 群 verify 不生成或发送 token 明文；aiocqhttp 提示 rotate，QQ 官方提示 private confirm。

所有普通聊天文本必须执行 URL 与 Token 零容忍：不得包含 `http://`、`https://`、configured domain、`public_base_url` 值、完整 Webhook URL、`OMP_SESSION_WEBHOOK_URL=...` 或符合插件 `whn_` 格式的 Token 明文。意外 Token 替换为 `[Token 已隐藏]`；只有 private create、rotate、QQ 官方 confirm 第二条专用凭据安全类型绕过 sanitizer。允许展示字段名为 `Endpoint Path` 的相对路径。

pending `expires_at` 缺失、无时区或无法解析时必须 fail-closed：关联 endpoint 标记为 `expired`、清理 pending 并持久化。QQ 官方 verify 与 confirm 均重查同一不可延长 expiry；waiting-owner 过期同样清理。aiocqhttp verified-but-unrotated endpoint 返回 `403 token_unclaimed`。

Registry v2 的 mutating API 使用锁内 candidate transaction：先复制并校验候选快照，持久化成功后才发布内存状态；写入失败时内存和磁盘保持旧快照。迁移、create、verify/confirm、rotate、revoke、delete 与管理员撤销遵循同一原子和 fail-closed 原则。

---

## 日志与脱敏

日志允许记录：

- provider。
- event。
- endpoint。
- 目标名称。
- 渲染模式。
- 错误类型和阶段。

日志禁止记录：

- Bearer Token 原文。
- Authorization header 原文。
- token 哈希完整值。
- 完整 raw payload。
- 完整 prompt。
- 带 query 和 fragment 的 URL。

URL 日志应去除 query 和 fragment。

---

## 安全规格

### Body Size 限制

MVP 推荐默认最大 body：

```text
256 KiB
```

超过限制返回 413。

### Content-Type 限制

只接受：

```text
application/json
application/*+json
```

### Token 与目标授权限制

- token 必须由插件生成，使用足够随机的不可预测值。
- token 持久化时必须保存哈希，不保存明文。
- token 明文只在 private create、rotate 或 QQ 官方 confirm 成功时私聊发送给申请者。
- endpoint 必须绑定 owner user id。
- 私聊 endpoint 只能投递给申请者私聊。
- 群聊 endpoint 必须经过群内验证后创建。
- 群聊验证按 adapter 分流：aiocqhttp 要求原申请者在预指定群为 owner/admin；QQ 官方允许目标群任一 owner/admin 批准，原申请者在后续同平台 private confirm 中单独校验。
- payload 不允许直接指定任意 UMO。
- payload 中的目标选择只能是 endpoint target whitelist 内的 alias。

### 平台投递风险限制

- OneBot/NapCat 的主动私聊存在真实风控风险，默认不得主动发送 Webhook 私聊通知。
- QQ 官方 Bot 的主动私聊和主动消息受官方规则与额度限制，不能作为无限安全的替代通道；管理员确认当前官方规则与额度后方可开启。
- 插件不实现、不记录也不建议任何对抗或规避平台风控的方案。
- 当前 UMO 不能可靠识别具体 OneBot 实现，因此不得自动宣称或推断目标由 NapCat 承载。
- 平台差异统一由 Sender 投递策略处理，当前不分叉仓库。详细依据见 [`platform-delivery-policy.md`](platform-delivery-policy.md)。

### 群管理员校验限制

MVP 默认使用群消息事件进行校验，而不是依赖私聊上下文查询群角色。

校验策略按 adapter 固定：

- `aiocqhttp` 必须通过 `await event.get_group()` 获取群资料，并比对 owner/admin 成员；不得使用 `event.is_admin()` 代替群角色。
- `qq_official` 仅使用当前群事件 raw payload 中的 `group_openid`、`member_openid` 和 `member_role`。
- 如果当前适配器无法提供权威群角色信息，则群聊 token 申请应失败并提示无法校验。

### HTML 模板限制

- 模板只能由插件管理员配置。
- 默认模板必须自包含。
- 默认不依赖外部 JS、CSS、远程字体和 CDN 图片。
- 不提供聊天命令上传模板。
- 不接受管理员指定的模板路径；模板 ID 和 revision 文件名均由后端生成。
- 拒绝危险标签、事件属性、危险 CSS、脚本 URL 和外部资源。

#### HTML 转义决策

当前 HTML renderer 使用 Jinja2 `SandboxedEnvironment` 并启用 `autoescape`。sandbox 清空默认 globals，只保留模板内部循环计数所需的 `namespace` helper；上下文根仅为 `event`。渲染结果还会经过 HTML policy 校验并注入限制性 CSP。

### Raw Payload 使用限制

- renderer 默认不展示 raw payload。
- README 和日志中不得输出真实 payload 中的敏感信息。
- 后续如果支持调试模式，需要显式开启。

---

## 错误处理规格

### 鉴权错误

鉴权错误不应泄露正确 token 是否存在。

响应：

```json
{
  "code": 1,
  "message": "unauthorized",
  "data": {
    "error": "invalid_token"
  }
}
```

### Provider 解析错误

解析错误应返回 `invalid_payload` 或 `unsupported_event`。

日志只记录缺失字段路径和事件摘要。

### 渲染错误

渲染错误分阶段记录：

- `template`
- `html_render`
- `image_validate`
- `message_build`

### 发送错误

发送错误记录：

- target name。
- UMO 类型摘要。
- error type。

不记录完整 payload。

策略性跳过不属于发送错误，不记录 error stack，不进入失败重试。日志可记录目标名称、`FriendMessage` 类型、`status=skipped` 与 `reason=private_notifications_disabled`。

---

## 验收用例

### 状态命令

| 用例 | 输入 | 预期 |
| --- | --- | --- |
| 查看完整状态 | `<唤醒词>webhook_notifier` | 返回插件状态摘要 |
| 查看短状态 | `<唤醒词>whn` | 返回插件状态摘要 |

### HTTP 鉴权

| 用例 | 输入 | 预期 |
| --- | --- | --- |
| 缺少 Authorization | POST JSON | 401 `missing_authorization` |
| Token 错误 | `Authorization: Bearer wrong` | 401 `invalid_token` |
| Token 正确 | `Authorization: Bearer <token>` | 继续处理 |
| 已撤销 Token | 使用 revoked endpoint token | 403 `endpoint_revoked` |

### Token 申请与权限

| 用例 | 输入 | 预期 |
| --- | --- | --- |
| 申请私聊 token | 私聊 `<唤醒词>whn token new private` | yield 安全摘要，再 direct send Token Plain |
| 私聊 token 列表 | 私聊 `<唤醒词>whn token list` | 只展示自己的 endpoint 摘要，不展示 token 明文 |
| aiocqhttp 申请群聊 token | 私聊 `<唤醒词>whn token new group <数字群号>` | 创建 `prebound_group` 待验证申请 |
| qq_official 申请群聊 token | 私聊 `<唤醒词>whn token new group current` | 创建 `bind_current_group` 待验证申请 |
| aiocqhttp 群管理员验证 | 原申请者在预指定群执行 verify | tokenless active、清理 pending，提示原申请者私聊 rotate |
| qq_official 群管理员验证 | 目标群任一 owner/admin 执行 verify | record 仍 pending，转 waiting-owner 且不清理 pending，提示原申请者私聊 confirm |
| qq_official 原申请者确认 | 私聊 `<唤醒词>whn token confirm <request_id>` | active，yield 摘要，再 direct send Token Plain |
| 普通群成员验证 | 普通成员执行 verify | 验证失败，不创建 token |
| aiocqhttp 非申请者验证 | 其他用户执行 verify | 验证失败 |
| qq_official 非申请者群管理批准 | 目标群其他 owner/admin 执行 verify | 群批准成功，仍等待原申请者 private confirm |
| QQ 官方确认消息发送失败 | confirm 已提交但 Token Plain 发送失败 | 不回滚 active；原申请者私聊 rotate 恢复 |
| Bot 不在目标群 | 申请群聊 token | 申请失败或无法验证 |
| 轮换 token | 私聊 `<唤醒词>whn token rotate <endpoint_name>` | 旧 token 失效，yield 摘要，再 direct send 新 Token Plain |

敏感 direct send 只调用 adapter 一次，不自动重试，也不宣称网络 exactly-once。adapter 抛出异常时 Registry 已提交状态保持不变，返回无凭据恢复提示；adapter 返回 `None` 仅表示调用完成，不额外声称用户已确认收到。
| 撤销 token | 私聊 `<唤醒词>whn token revoke <endpoint_name>` | endpoint 被撤销，后续请求不可用 |

### OMP 事件

| 用例 | 输入 | 预期 |
| --- | --- | --- |
| 标准 `session_stop` | OMP payload | 生成标准化事件 |
| Header 与 body 不一致 | `X-OMP-Event: session_stop` + body 其他 event | 400 `invalid_payload` |
| 未知事件 | `event: omp.unknown` | 400 `unsupported_event` |
| 缺少可选字段 | 缺少 `round.lastAssistant` | 仍可生成通知 |

### 渲染与发送

| 用例 | 输入 | 预期 |
| --- | --- | --- |
| text 模式 | 标准事件 | 发送纯文本 |
| html_image 模式成功 | 标准事件 + T2I 正常 | 发送图片 |
| html_image 模式失败且 fallback 开启 | T2I 异常 | 发送纯文本并标记 fallback |
| 目标 UMO 错误 | 配置错误 UMO | HTTP 200 `partial_failure`，`retryable=true`，逐目标错误写入 `send_results` |
| payload 指定白名单内 target alias | endpoint 绑定多个 target | 只发送到该 alias 对应目标 |
| payload 指定白名单外 target alias | endpoint 未绑定该 target | 拒绝请求或忽略该 target，不发送到未授权目标 |
| 默认私聊安全策略 | `enable_private_notifications=false`，目标仅含 `FriendMessage` | HTTP 200 `skipped`，`delivered=false`、`rendered=false`、`retryable=false`，不调用 renderer/sender |
| 恢复已有私聊 endpoint | 开启 `enable_private_notifications` 并 reload | 原 endpoint 与 Token 恢复投递，无需重建或轮换 |
| 混合群聊与私聊目标 | 开关关闭，群聊发送成功 | 群聊 delivered、私聊 skipped，HTTP 200 `partial_delivery`、`retryable=false` |
| 真实发送失败 | 至少一个可投递目标调用发送 API 后失败 | HTTP 200 `partial_failure`；对应项 `ok=false`，并通过 `delivered`、`retryable=true` 与 `send_results` 描述 |

### WebUI 模板管理

| 用例 | 输入 | 预期 |
| --- | --- | --- |
| 查看内置模板 | 打开 Plugin Page 并选择 built-in | 内容可查看，保存和删除不可用 |
| 新建模板 | 从 built-in 新建副本并保存 | 后端生成 custom ID 与 revision 1 |
| 更新模板 | 携带当前 `expected_revision` 保存 | 生成新 revision 文件并更新 snapshot |
| 并发冲突 | 携带过期 `expected_revision` 保存、应用或删除 | 返回 409，页面提示重新载入 |
| 保存 active 模板 | 修改 active 自定义模板后仅保存 | active ID 不变，新内容立即生效 |
| 保存并应用 | `apply=true` 保存 | 保存和 active 切换在同一次 registry 提交完成 |
| 安全预览 | 提交合法 HTML 与 JSON event | 不持久化，返回带 CSP 的 HTML，并在 sandbox `srcdoc` 展示 |
| 危险模板 | 包含脚本、事件属性、外部资源或危险 CSS | 保存或 preview 被拒绝 |
| 自定义模板失败 | active custom 渲染失败 | 尝试 built-in；仍失败时按配置回退 text |
| 画布宽度 | 保存 `canvas_width=960` 并渲染 | T2I `viewport_width` 使用 960，裁剪使用同一宽度 |

---

## 与 PRD 的对应关系

| PRD 能力 | FSD 对应章节 |
| --- | --- |
| Webhook 接收 | HTTP 接口规范 |
| Bearer Token 鉴权 | Auth、HTTP 接口规范、安全规格 |
| Token 模型取舍 | Token Model Decision、Simple Mode Evaluation |
| 用户自助申请 token | Token Provisioning、状态命令规格、安全规格 |
| 多用户多 endpoint | Endpoint Registry、路由规格、配置规格 |
| OMP 首个集成 | OMP Provider 规格 |
| 标准化事件 | 标准化事件对象规格 |
| QQ 群聊 / 私聊投递 | 路由规格、发送规格 |
| 私聊通知安全默认值 | Sender、成功响应、配置规格、安全规格 |
| HTML 卡片图片 | 渲染规格 |
| 文本兜底 | 渲染规格、错误处理规格 |
| WebUI 模板管理 | Template Registry 与 Plugin Page、Template Bridge API 契约 |
| 状态可观测 | 状态命令规格、日志与脱敏 |

---

## 已收敛决策

FSD v0.1.0 对 MVP 功能契约作出以下决策：

| 议题 | MVP 决策 | 后续方向 |
| --- | --- | --- |
| Webhook HTTP server | 使用插件内独立 HTTP server，默认监听 `127.0.0.1`；公网暴露交给 Nginx、Caddy、Cloudflare Tunnel 或部署层，公网访问必须使用 HTTPS，TLS 终止不由插件负责 | 若 AstrBot 后续提供稳定统一 HTTP 插件入口，可评估迁移 |
| Endpoint Registry 存储 | 使用插件数据目录下的 JSON 文件，例如 `data/webhook_tokens.json`；token 只存哈希 | endpoint 数量、审计或并发写入需求上升后评估 SQLite |
| UMO 校验 | 启动或配置加载时校验 UMO 字符串格式；实际可达性在发送时确认 | 后续如 AstrBot 提供会话探测 API，可增加主动校验 |
| HTML 静态资源 | MVP 默认模板自包含，不支持任意本地静态资源目录 | 后续可设计受限资源目录和路径白名单 |
| 默认渲染模式 | 默认 `text`；`html_image` 必须由管理员显式开启；MVP 全局生效 | 后续如需 endpoint 级覆盖，应重新设计独立 override 字段，不复用旧 `render_mode` |
| Request ID | 每个 Webhook 请求生成 request id，并贯穿响应、日志、渲染和发送结果 | 后续可接入持久化审计日志 |
| Token 哈希 | 使用 `HMAC-SHA256(server_secret, token)`，token 明文只展示一次 | 后续如需要更强抗暴力破解能力，可评估 Argon2/bcrypt，但需考虑依赖和性能 |
| 群聊验证码 | `request_id` 使用 UUID4，`code` 使用 6 位小写十六进制，默认 10 分钟有效且一次性使用 | 后续可按风险增加速率限制和验证码长度 |
| 群管理员校验 | MVP 使用群内验证命令，不依赖私聊上下文查询群角色 | 适配器原生群成员查询可作为可选优化 |
| Simple mode | 不进入 MVP；MVP 只支持 managed endpoint/token 模型 | 如需要，后续新建 issue 独立定义安全边界和迁移路径 |
| Webhook 私聊通知 | `enable_private_notifications=false`；仅跳过 `FriendMessage` 状态通知，不改 registry 与 Token | 管理员确认平台规则与风险后显式开启 |
| 平台架构 | 单仓、UMO 统一路由、Sender 集中策略；不根据 UMO 自动识别 NapCat | 出现第二个真实 adapter 差异后再评估 `core/delivery_policy.py` 能力层 |

---

## 后续待评估项

以下事项不阻塞 MVP 功能契约：

- 是否提供 UMO WebUI 辅助生成器。
- 是否支持本地静态资源目录及资源打包。
- 是否提供 SQLite backend。
- 是否提供 simple mode。
- 是否支持适配器原生群成员查询以减少群内验证步骤。
- 第二个真实 adapter 差异是否足以触发独立 delivery policy 能力层。

---

## 版本变更记录

### v0.1.0

- 初始 FSD Draft。
- 定义 MVP 功能模块、HTTP 接口、OMP provider、标准化事件、渲染、发送、配置、安全和验收用例。
- 补充用户自助申请 Webhook Token、多 endpoint、目标白名单和群管理员验证规格。
- 补充 endpoint/token 模型取舍，并明确 simple mode 不进入 MVP。
- 收敛 MVP 开放问题，补充 HTTP server、endpoint registry、UMO 校验、默认渲染模式和 request id 等决策。
- 补充 token 哈希算法、群聊验证码格式、模板变量命名空间、错误码、配置优先级、body size 检查和 token 生命周期细节。

### v0.2.0 — MS2 HTML 卡片

- 增加默认自包含 HTML 卡片模板（designer 设计）。
- `renderer.py`：增加 `DEFAULT_HTML_TEMPLATE`、`render_html()`、`render_html_default()`、`render_html_data()`、`validate_image_result()`。
- `sender.py`：增加 `send_image()`，支持 URL/base64/data URL/path/bytes Image 组件构造。
- `server.py`：`WebhookServer` 接收 `html_render` 回调和 `plugin_config`；新增 `_handle_html_image()` / `_handle_text()` / `_fallback_to_text()` 分支；响应新增 `render_mode`、`requested_render_mode`、`fallback_to_text`、`fallback_reason` 字段。
- `main.py`：放开 `html_image` 的 MS1 自动降级；`WebhookServer` 传入 `html_render` 回调和配置字典。
- 图片结果校验支持 PNG（ `\x89PNG` ）、JPEG（ `\xff\xd8\xff` ）、WebP（ `RIFF....WEBP` ）magic number。
- 单元测试覆盖 renderer、sender、server 各链路。
- 移除 `EndpointRecord.render_mode` 与 `template` 字段。渲染模式统一使用插件全局配置。旧 registry JSON 中的残留值自然忽略，重新保存后不再保留。
- 交付 Plugin Page 模板管理 Phase 0/1：本地 Monaco 0.52.2、Vite 6.4.3、4 个 inline workers、`asset_token` 与 sandbox 验证，以及模板列表、编辑、preview、保存、应用和删除。
- 新增 version 1 Template Registry、不可变 revision 文件、原子 registry 提交、`expected_revision` 409 冲突控制和全局 active 模板。
- HTML 渲染启用 Jinja sandbox 与 autoescape，增加 HTML/CSS/外部资源策略、CSP、preview 限制、自定义模板到 built-in 再到 text 的回退，以及模板 `canvas_width` 覆盖。
- 新增 `enable_private_notifications=false` 安全默认值、投递 preflight 与 Sender 兜底职责；私聊跳过不修改 Endpoint Registry。
- 明确 all skipped 与 mixed targets 的 HTTP 200 响应语义，以及只有实际调用发送 API 后的真实发送失败才设置 `retryable=true`。
