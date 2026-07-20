# Webhook Notifier 命令参考

本文面向使用 Webhook Notifier 创建和管理 Endpoint 的普通用户与 AstrBot 管理员。所有示例均使用安全占位符，不包含真实 Token、Endpoint Path、平台身份、群标识或目标 UMO。

---

## 唤醒词与命令根

本文统一用 `<唤醒词>` 表示当前会话的 AstrBot `wake_prefix`，短命令根为 `<唤醒词>whn`，长命令根为 `<唤醒词>webhook_notifier`。

- 默认配置 `wake_prefix=["/"]`：使用 `/whn ...`。
- 自定义为 `wake_prefix=["!"]`：使用 `!whn ...`。
- 配置为空列表或第一个前缀为空字符串：使用裸命令 `whn ...`。
- 配置多个前缀时，帮助和提示采用列表中的第一个前缀。
- 若配置缺失、类型错误、元素不是字符串、包含控制字符或读取失败，运行时帮助使用 `<AstrBot唤醒词>whn` 安全占位符，并提示检查 AstrBot 配置和插件日志；不会擅自回退为 `/`。

本文后续示例使用静态占位符 `<唤醒词>`，不要把异常占位符 `<AstrBot唤醒词>` 当作实际命令输入。

---

## 命令总览

“全局超级管理员”指 AstrBot 的全局超级管理员，不是普通群管理员。除 `verify` 外，Token 创建与用户管理命令均应在私聊执行。

| 命令 | 执行场景 | 权限 | 支持 adapter | 作用 |
| --- | --- | --- | --- | --- |
| `<唤醒词>whn` | 私聊、群聊 | 所有人 | `aiocqhttp`、`qq_official` | 查看状态 |
| `<唤醒词>webhook_notifier` | 私聊、群聊 | 所有人 | `aiocqhttp`、`qq_official` | 查看完整状态 |
| `<唤醒词>whn status` | 私聊、群聊 | 所有人 | `aiocqhttp`、`qq_official` | 查看状态 |
| `<唤醒词>whn help` | 私聊、群聊 | 所有人 | `aiocqhttp`、`qq_official` | 查看图片或纯文本帮助 |
| `<唤醒词>whn token new private [名称]` | 仅私聊 | 当前用户 | `aiocqhttp`、`qq_official` | 创建绑定当前私聊的 Endpoint |
| `<唤醒词>whn token new group <数字群号> [名称]` | 仅私聊 | 当前用户 | 仅 `aiocqhttp` | 发起预绑定数字群号的群验证 |
| `<唤醒词>whn token new group current [名称]` | 仅私聊 | 当前用户 | 仅 `qq_official` | 发起“验证时绑定当前群”的群验证 |
| `<唤醒词>whn token verify <request_id> <code>` | 仅目标群 | 群主或群管理员；`aiocqhttp` 还必须是原申请者 | `aiocqhttp`、`qq_official` | 完成群管理员验证阶段 |
| `<唤醒词>whn token confirm <request_id>` | 仅私聊 | QQ 官方原 C2C 申请者 | 仅 `qq_official` | 确认群绑定、激活并领取 Token |
| `<唤醒词>whn token list` | 仅私聊 | 当前用户 | `aiocqhttp`、`qq_official` | 列出当前平台 scope 内可见 Endpoint |
| `<唤醒词>whn token rotate <名称>` | 仅私聊 | Endpoint owner | `aiocqhttp`、`qq_official` | 生成新 Token，并立即使旧 Token 失效 |
| `<唤醒词>whn token revoke <名称>` | 仅私聊 | Endpoint owner | `aiocqhttp`、`qq_official` | 软撤销并保留审计记录 |
| `<唤醒词>whn token delete <名称>` | 仅私聊 | Endpoint owner | `aiocqhttp`、`qq_official` | 永久删除终态记录 |
| `<唤醒词>whn admin token list` | 仅私聊 | 全局超级管理员 | `aiocqhttp`、`qq_official` | 查看 managed Endpoint 的最小管理元数据 |
| `<唤醒词>whn admin token revoke-path <endpoint-path>` | 仅私聊 | 全局超级管理员 | `aiocqhttp`、`qq_official` | 按完整 Path 精确撤销，包括 quarantine kill switch |
| `<唤醒词>whn admin token revoke-owner <platform_id> <owner_user_id> <名称>` | 仅私聊 | 全局超级管理员 | `aiocqhttp`、`qq_official` | 按平台、owner 与名称精确撤销 managed Endpoint |

---

## 状态与帮助

### 状态命令

以下命令均可查看插件状态：

```text
<唤醒词>whn
<唤醒词>whn status
<唤醒词>webhook_notifier
```

状态包括插件启用状态、HTTP 服务状态、可投递 Endpoint 数量、渲染模式、文本降级、私聊通知开关、监听 IP、端口和基础路径。聊天中不会显示完整 Base URL；固定提示为“Base URL：请在 Plugin Page 中复制”。

### 帮助命令

```text
<唤醒词>whn help
<唤醒词>whn 帮助
```

帮助优先渲染为插件内置图片卡片，不读取 Webhook 通知的 active 模板。HTML/T2I 不可用时自动回退为结构化纯文本。普通用户只看到用户命令；全局超级管理员会额外看到管理员命令。

---

## 私聊 Endpoint

在与 Bot 的私聊中执行：

```text
<唤醒词>whn token new private <ENDPOINT_NAME>
```

名称可省略，插件会使用默认名称。成功后：

1. 正常命令回复给出名称、`Endpoint Path` 和前往 Plugin Page 复制 Base URL 的提示。
2. 插件通过当前私聊事件单独发送一次只含 Bearer Token 的纯文本消息。
3. Base URL、Endpoint Path 和 Token 需要在外部系统中分别保管和组合配置。

若管理员未开启 `enable_private_notifications`，Endpoint 和 Token 仍会创建且可鉴权，但 Webhook 私聊状态通知会被安全跳过。该开关不影响命令回复和 Token 交付。

---

## aiocqhttp 群聊流程

`aiocqhttp` 使用数字群号预绑定目标群。

### 1. 原申请者私聊发起申请

```text
<唤醒词>whn token new group <NUMERIC_GROUP_ID> <ENDPOINT_NAME>
```

- 群号必须全部为数字。
- 命令返回一次性的 `<REQUEST_ID>` 与 `<VERIFICATION_CODE>`。
- 此时 Endpoint 为 `pending_verification`，尚无可用 Token。

### 2. 原申请者到预指定群验证

```text
<唤醒词>whn token verify <REQUEST_ID> <VERIFICATION_CODE>
```

执行者必须同时满足：

- 是原私聊申请者；
- 当前群就是预指定群；
- 在当前群具有群主或群管理员身份。

验证成功后，pending 被清理，Endpoint 进入 **tokenless `active`**：记录已经激活，但尚未领取 Token，因此 Webhook 鉴权会返回 `403 token_unclaimed`。

### 3. 原申请者私聊领取 Token

```text
<唤醒词>whn token rotate <ENDPOINT_NAME>
```

`rotate` 生成首个可用 Token。凭据只在当前私聊中独立发送，不会在群内出现。

---

## qq_official 普通群双通道流程

`qq_official` 不接受数字群号或手工填写的群标识，只接受字面量 `current`，并把群管理员批准与原申请者确认拆成两个通道。

### 1. 原 C2C 申请者私聊发起申请

```text
<唤醒词>whn token new group current <ENDPOINT_NAME>
```

命令返回 `<REQUEST_ID>` 与 `<VERIFICATION_CODE>`。目标群将在下一阶段按实际群消息绑定。

### 2. 目标群管理员批准当前群

目标群内任一群主或群管理员可执行：

```text
<唤醒词>whn token verify <REQUEST_ID> <VERIFICATION_CODE>
```

- 批准者不要求是原 C2C 申请者。
- 插件只依据当前 QQ 官方群事件中的群标识与 `owner` / `admin` 角色进行校验；缺失或未知角色会拒绝验证。
- 验证后 Endpoint **仍是 `pending_verification`**，pending 进入 `group_verified_waiting_owner`，不生成 Token，也不清理 pending。

### 3. 原申请者回到同一 Bot 私聊确认

```text
<唤醒词>whn token confirm <REQUEST_ID>
```

只有原 C2C 申请者能在同一 `platform_id` scope 内确认。成功后才会：

- 绑定已批准的普通群；
- 激活 Endpoint；
- 删除 pending；
- 独立发送一次 Token。

群内验证与私聊确认共用创建时的同一到期时间，不会因进入第二阶段而延长。若 confirm 已成功提交但 Token 消息发送失败，Endpoint 不回滚，用户应私聊执行 `rotate` 恢复凭据。

---

## 用户管理命令

以下命令均仅限私聊，并严格限制在当前 `platform_id + owner + 名称` scope 内。

### 列表

```text
<唤醒词>whn token list
```

列表展示当前 scope 内用户可见的 Endpoint 名称、Path、状态、目标别名和时间摘要。已撤销或已过期记录不在默认用户列表中显示；quarantine 记录也不可见。

### 轮换

```text
<唤醒词>whn token rotate <ENDPOINT_NAME>
```

成功后旧 Token 立即失效，新 Token 通过独立纯文本消息交付。该命令也用于：

- aiocqhttp 群验证后的首次 Token 领取；
- 创建、confirm 或上次 rotate 的 Token 消息发送失败后的恢复。

### 撤销

```text
<唤醒词>whn token revoke <ENDPOINT_NAME>
```

这是软撤销：Endpoint 停止鉴权和投递，但保留终态记录与审计信息。需要彻底移除记录时，再执行 `delete`。

### 永久删除

```text
<唤醒词>whn token delete <ENDPOINT_NAME>
```

`delete` 是不可恢复的 hard delete，仅允许删除当前 scope 内状态为 `revoked` 或 `expired` 的 managed Endpoint。

- `active`：必须先执行 `revoke`。
- `pending_verification`：不能强制删除；应完成验证、等待过期，或先撤销。
- quarantine：普通用户不能发现、管理或删除。
- 其他平台或其他 owner 的同名记录：按不存在处理，不会跨 scope 推断。

删除后原 Path 返回 404，旧 Token 永久无效；同名 Endpoint 可以重新创建。确定性 Path 可能被复用，但新建记录会生成全新的 Token，不会恢复旧 Token 权限。

---

## 管理员 Registry 命令

管理员命令仅限 **AstrBot 全局超级管理员** 在私聊执行。普通群管理员没有此权限。

### 列出 managed Endpoint

```text
<唤醒词>whn admin token list
```

单次最多显示 50 条 managed Endpoint 的最小管理元数据。输出不会显示 Token 明文、Token hash、验证码或完整目标 UMO，也不会列出 quarantine。

### 按 Path 精确撤销

```text
<唤醒词>whn admin token revoke-path <ENDPOINT_PATH>
```

- 必须提供完整 Endpoint Path；可省略开头的一个 `/`。
- 只做精确匹配，不支持前缀、包含或其他模糊匹配。
- 这是关闭 quarantine legacy Endpoint 的管理员 kill switch。

### 按 owner 精确撤销

```text
<唤醒词>whn admin token revoke-owner <PLATFORM_ID> <OWNER_USER_ID> <ENDPOINT_NAME>
```

选择器由 `platform_id + owner_user_id + 名称` 共同组成。名称按普通 Endpoint 名称规则规范化；命令不会跨平台猜测 owner，也不支持模糊匹配。

管理员命令的审计日志使用安全摘要或不可逆标识，避免记录 Token、hash、验证码和完整目标 UMO。运维工单仍应主动替换真实平台身份、Path 与群信息。

---

## 常见错误与恢复

| 现象 | 原因 | 恢复方式 |
| --- | --- | --- |
| 创建、confirm 或 rotate 后未收到 Token | direct send 失败或送达状态不确定；操作不会回滚，也不会自动重试 | 在同一平台私聊执行 `<唤醒词>whn token rotate <ENDPOINT_NAME>` |
| aiocqhttp 群验证后请求返回 `token_unclaimed` | Endpoint 已 tokenless active，但尚未领取 Token | 原申请者私聊执行 `rotate` |
| `active endpoint 不能永久删除` | hard delete 只接受终态记录 | 先执行 `revoke`，再执行 `delete` |
| `endpoint 不存在` 或管理员选择器未命中 | 当前平台、owner 或名称 scope 不一致，或记录已删除 | 回到创建 Endpoint 的同一 Bot/adapter；核对精确名称，不要尝试跨 scope 猜测 |
| `验证请求不存在或已过期` | request 不属于当前 `platform_id`，已消费，或超过有效期 | 重新发起群聊申请；旧验证码不能复用 |
| QQ 官方群已批准但 confirm 失败 | 非原申请者、不同 `platform_id`、phase 不正确或共同 expiry 已过期 | 由原申请者回到同一 Bot 私聊；若已过期则重新申请 |
| aiocqhttp verify 被拒绝 | 群不匹配、执行者不是原申请者，或不是群主/管理员 | 由原申请者在预指定群以正确角色重新执行 |
| QQ 官方 verify 被拒绝 | 不是普通群事件，或群身份字段/角色无法安全校验 | 确认使用受支持的 `qq_official` WebSocket 普通群，并由群主/管理员执行 |

---

## 安全占位符示例

以下仅演示参数位置：

```text
<唤醒词>whn token new private <ENDPOINT_NAME>
<唤醒词>whn token new group <NUMERIC_GROUP_ID> <ENDPOINT_NAME>
<唤醒词>whn token new group current <ENDPOINT_NAME>
<唤醒词>whn token verify <REQUEST_ID> <VERIFICATION_CODE>
<唤醒词>whn token confirm <REQUEST_ID>
<唤醒词>whn token rotate <ENDPOINT_NAME>
<唤醒词>whn admin token revoke-path <ENDPOINT_PATH>
```

不要把真实 Token、完整 URL、Endpoint Path、平台身份、群标识或 UMO 粘贴到公开聊天、截图、日志、Issue 或文档中。

---

## 相关文档

- [安全与运维指南](security-and-operations.md)
- [平台投递策略](platform-delivery-policy.md)
- [platform_id 离线 Rebind Runbook](platform-id-rebind-runbook.md)
