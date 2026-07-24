# OpenCode 集成指南

本文说明 OpenCode CLI/Desktop Plugin 与 Webhook Notifier 的 V1 接入边界。文中的路径、URL、Token 和环境变量值均为占位符；不要把真实部署值复制到公开文档、Issue、日志或截图中。

## 版本基线与验证目标

- OpenCode Plugin API 基线：`v1.17.9`。
- 当前实际本机 CLI/Desktop 验证目标：`v1.18.4`。
- 集成入口是 OpenCode V1 file Plugin 的 `default { id, server }` 形态，不是旧版 `tui`、裸函数或任意自定义导出。
- Python 服务端使用 Endpoint 的 Bearer 鉴权和 `opencode` provider；创建后 provider 不可变。

`v1.17.9` 是本插件 TypeScript 类型和调用约定的 API 基线，`v1.18.4` 是本机 smoke 使用的实际运行时版本。升级 OpenCode 后应重新执行 CLI smoke，并观察事件、配置插值和 Plugin Service 加载行为。

OpenCode 的模型档位使用可选的安全字段 `modelVariant`：优先读取 Assistant `message.updated.properties.info.variant`，缺失时读取 `session.model.variant`。该字段只表示 OpenCode runtime 暴露的 `variant`/`modelVariant`，不保证等同于 provider 原始 API 的 `reasoning_effort` 或 `reasoningEffort`；插件不会根据 provider 或 model 名称推断档位。

---

## 完整端到端验收顺序

OpenCode Desktop 测试依赖已经部署的新 AstrBot 服务端和一条真实的 `provider=opencode` Endpoint。不能只把 TypeScript Plugin 放进 OpenCode 后重启，也不能把 CLI loader smoke 当作完整联调结果。

按以下顺序执行：

1. 先部署并重载包含 `session.scope` allowlist 和通知策略的新版 AstrBot 服务端；旧服务端严格 allowlist 不接受新字段。
2. 使用新版本号构建 AstrBot 插件测试 ZIP，校验 ZIP 根目录、版本字段、文件清单和 checksum。
3. 在测试 AstrBot 中安装该 ZIP 并重载 Webhook Notifier，确认运行版本已经更新。
4. 与 Bot 交互创建 `provider=opencode` Endpoint，从认证 Plugin Page 获取 Base URL，并分别取得 Endpoint Path 与一次性交付的 Bearer Token。
5. 在运行 OpenCode 的机器部署 `webhook-notifier.ts`，把组合后的完整 Endpoint URL 与 Token 通过 env/file 插值交给 Plugin。
6. 部署新版 OpenCode Client 后，完全退出并重新启动 OpenCode Desktop 或 CLI，使 file Plugin 和配置进入新进程。
7. 依次验证普通任务结束、权限请求和模型/API 失败，核对 `completed`、`action_required`、`failed` 通知及隐私边界。
8. 记录测试包版本、commit、OpenCode 版本、AstrBot 返回状态和脱敏结论，再决定是否关闭集成 Issue 或发布正式版本。

仓库中的 CLI smoke 只覆盖第 5 至第 6 步中的 Plugin Service 加载能力，不会创建 AstrBot Endpoint，不会取得真实 URL/Token，也不会验证 Bot 投递。

---

## AstrBot Endpoint

在 AstrBot 私聊中创建 OpenCode 专用 Endpoint：

```text
<唤醒词>whn token new private <名称> --provider opencode
```

群 Endpoint 也可以在创建时使用同一 provider 参数，群验证流程仍遵循当前 adapter 的规则。未指定 `--provider` 时默认使用 `omp`：

```text
<唤醒词>whn token new private <名称>
```

provider 只在创建时选择，允许值是 `omp` 与 `opencode`。它会写入 Endpoint 公共记录，后续 `rotate`、`revoke`、`delete` 不会改变 provider，也没有把已有 Endpoint 原位改成另一个 provider 的命令。需要切换 provider 时，按原 Endpoint 的撤销/删除规则处理后重新创建。

创建成功后分别保存：

1. 已认证 Plugin Page 提供的 Base URL；
2. 聊天回复中的 Endpoint Path；
3. 单独 direct send 交付的 Bearer Token。

不要把 Token 拼进 URL，也不要让 OpenCode payload 自己指定目标 UMO。Endpoint 的目标白名单仍由 AstrBot Registry 决定。

---

## 正确的 Plugin tuple 与 V1 server

OpenCode 配置顶层的 `plugin` 必须是 tuple 数组。每一项是：

```text
[模块 URL, options 对象]
```

示例（值均为占位符）：

客户端可在 options 中设置可选的 `instanceDisplayName`、`actionContentMode` 和 `metadataDiagnostics`。`instanceDisplayName` 用于标识 OpenCode 实例；`projectName` 由客户端自动推导 worktree basename，不需要用户配置：

```jsonc
{
  "instanceDisplayName": "OpenCode Desktop",
  "actionContentMode": "strict",
  "metadataDiagnostics": "off"
}
```

`actionContentMode` 可选 `strict`（默认，仅类别/计数）、`summary`（清洗截断摘要）和 `full`（显式白名单内容）。`full` 是显式 opt-in，可能泄露业务文本或目标路径。

`metadataDiagnostics` 可选 `off`（默认）、`once` 或 `sample`；非法值按 `off` 处理。启用 `once` 后，Plugin 进程生命周期内每个诊断阶段最多输出一条带统一前缀的单行安全 JSON；`sample` 每个阶段最多输出 8 条，对完全相同的安全 payload 去重，超过上限后静默停止。阶段包括 `message_updated`、`session_get`、实际调用的 `session_messages` 和最终 `outgoing_envelope`。`sample` 还会为有 session ID 的诊断分配仅存于内存的递增 `sampleSession`，同一匿名 session 在各阶段复用该数字，不输出 raw ID、匿名 ref/hash 或 message/parent ID。诊断只记录 bounded key 名、短字符串、存在性/长度、枚举状态和布尔值，不记录标题/名称正文、parent ID 值、消息正文、parts、路径、Token、URL、headers、response body 或异常 message。默认关闭时不增加诊断日志。

`actionContentMode` 与服务端全局 `notification_mode` 正交：前者只控制 Question/Permission 内容隐私，后者控制是否进入通知渲染/发送链路。服务端 `notification_mode=focused` 只抑制标准状态为 `completed` 的 `subagent` 与 `auxiliary`，`failed`、`action_required`、root、unknown 和未来未知状态均放行；`all` 保持全部通知。

```jsonc
{
  "plugin": [
    [
      "<PLUGIN_FILE_URL>",
      {
        "url": "{env:OPENCODE_WEBHOOK_URL}",
        "token": "{env:OPENCODE_WEBHOOK_TOKEN}",
        "timeoutMs": 5000,
        "enabled": true,
        "events": [
          "session_idle",
          "session_error",
          "permission_asked",
          "question_asked"
        ]
      }
    ]
  ]
}
```

不要写成以下不兼容形态：

- `"plugin": "<PLUGIN_FILE_URL>"`；
- `"plugin": [{"id": "webhook-notifier", "server": ...}]`；
- 直接把 `server` 函数作为 tuple 的第一个元素；
- 用旧的 `tui` 导出替代 `server`。

`integrations/opencode/webhook-notifier.ts` 的默认导出是 `{ id: "webhook-notifier", server }`。OpenCode 实际加载时调用：

```text
default.server(input, options)
```

`server` 返回包含 `event` hook 的 hooks 对象。插件使用 `input.client.session.get()` 做非关键的会话标题、scope、会话开始时间、agent 和 model 丰富；`session.time.created` 是唯一的 `startedAt` 来源。对 `session_idle`，插件优先使用本轮首次 `busy` 到 `idle` 的接收时间计算 `taskStartedAt`、`endedAt` 与 `durationMs`；只有本轮没有可靠 busy 周期时间时，才使用当前/最后 Assistant Message 的 `info.time.created` 和 `info.time.completed` 作为 fallback。`session.time.updated` 不参与任务结束时间或耗时计算。在 agent/model 或 Assistant 元数据仍缺失时最多调用一次 `input.client.session.messages({path:{id}, query:{limit:10}})` 做兼容 fallback。丰富失败不阻止事件发送。

仓库内的 `integrations/opencode/opencode.jsonc` 是可复制的配置模板，但复制到自己的配置目录时，应替换模块 URL，并只用下面的 env/file 方式提供凭据。

---

## URL 与 Token 的 env/file 插值

插件 options 支持：

```jsonc
{
  "url": "{env:OPENCODE_WEBHOOK_URL}",
  "token": "{env:OPENCODE_WEBHOOK_TOKEN}"
}
```

Token 也可以使用受控文件：

```jsonc
{
  "url": "{env:OPENCODE_WEBHOOK_URL}",
  "token": "{file:<TOKEN_FILE_PATH>}",
  "timeoutMs": 5000
}
```

- `{env:NAME}` 从 OpenCode 进程环境读取；`{file:PATH}` 读取文件并去除首尾空白。
- URL 和 Token 缺失或插值失败时，Plugin 安全禁用，不发送不完整请求。
- Token 文件应使用操作系统权限限制，避免进入仓库、备份公开目录、shell history 或日志。
- `OPENCODE_CONFIG_CONTENT` 适合隔离 smoke 或临时测试，不应把真实 Token 写入长期配置环境变量。
- 请求始终使用 `Authorization: Bearer <TOKEN>`；Token 不作为 query 参数或 URL 的一部分发送。

---

## 四事件与状态机

插件监听 OpenCode runtime event，向 AstrBot 发送固定 V1 envelope。服务端要求 `X-OpenCode-Event` 与 body 的 `event` 一致。

| OpenCode 输入 | Webhook 事件 | 处理方式 |
| --- | --- | --- |
| `session.status` 从 `busy` 到 `idle`，或兼容的 `session.idle` | `opencode.session_idle` | 当前工作周期只发送一次 |
| `session.error` | `opencode.session_error` | 立即发送，并抑制当前周期后续 idle |
| `permission.updated`，兼容 `permission.asked` | `opencode.permission_asked` | 同一 Session 内 150ms 窗口聚合，不改变 busy/idle 周期 |
| `permission.replied` | 不发送 | 在 150ms flush 前按 `requestID` 撤销对应权限请求 |
| `question.asked` | `opencode.question_asked` | 同一 Session 内 150ms 窗口聚合；内容模式由 `actionContentMode` 控制 |
| `question.replied`、`question.rejected` | 不发送 | 在 150ms flush 前按 `requestID` 撤销对应问题请求 |
| `message.updated` | 不发送 | 仅消费 `info.role=assistant` 的清洗后 agent/model、`variant` 与 `time.created/completed` 元数据；user 或 malformed 更新忽略 |

状态规则：

1. 初始 `idle` 没有先前 `busy` 时忽略，避免启动时重复通知。
2. `busy` 开始新周期，清除上一周期的 idle/error 抑制状态。
3. 一个周期的 `error` 已通知后，后续 `idle` 不再补发完成通知。
4. 同一周期的 `idle`、兼容旧 `session.idle` 和并发 idle 只允许一个通知通过。
5. permission 和 question asked 事件不依赖 busy/idle 状态，可在任意周期独立通知；同一 Session、同一类型使用固定 150ms 本地内存 debounce，不同 Session 或不同类型不合并。
6. flush 会先摘除 bucket，再进行异步 enrichment/build/send；回复只撤销尚未 flush 的成员。聚合 envelope 在 bucket 创建时生成稳定 ID，重试复用同一 ID；已发送通知不撤回。

---

## 匿名 ref 与 name fallback

OpenCode 原始 session ID 不离开 Plugin。Plugin 计算：

```text
session.ref = first_32_hex(SHA-256("opencode:" + raw_session_id))
```

服务端的 `NormalizedEvent` 只保留安全的短 ref 展示值，不保留原始 session ID。插件从 `input.worktree`、`input.project.worktree`、`input.directory` 按优先级取项目路径，只清洗并发送最后一级 `projectName`，根目录、`.`、空值或不可用时省略。会话名称经过以下处理：

- 删除 bidi、zero-width、format 和行/段分隔控制字符；
- 将控制字符、换行和连续空白归一化；
- 最多保留 200 个 Unicode 字符；
- 保留普通 Unicode、CJK、emoji 和普通 HTML/Markdown 字符，最终转义由服务端 renderer 负责。

通知主标题使用 `sessionName`；如果 `session.name`、`session.title` 都不可用或清洗后为空，回退为：

```text
OpenCode Session <ref12>
```

其中 `<ref12>` 只从匿名 ref 的 ASCII 字母、数字、`.`、`_`、`-` 构建，最多 12 个字符。该 fallback 是可读的匿名关联标识，不是原始 session ID。`source.name` 使用 `instanceDisplayName`，缺失时回退为 `OpenCode`；卡片详细字段增加可选的“项目”行，但不重复展示实例来源。

---

## 白名单与隐私边界

Envelope 只允许显式 V1 字段：`id`、`event`、`version`、`emittedAt`、`session`、可选 `instanceDisplayName`、`projectName`、`agent`、`model`、`modelVariant`、`durationMs`、`startedAt`、`taskStartedAt`、`endedAt`、`counts`、`permission`、`question`、`error`。`modelVariant` 是清洗、限长的安全字符串；它来自 OpenCode 的 Assistant `info.variant`，缺失时才来自 `session.model.variant`，不推断、不改名为原始 `reasoning_effort`。`session` 只允许 `ref`、`scope` 与清洗后的 `name`；`scope` 可为 `root`、`subagent`、`auxiliary` 或 `unknown`，缺失时服务端按 `unknown` 兼容。`parentID` 始终禁止进入 envelope；事件专属对象只允许声明过的标量、有限数组和计数，未知键 fail-closed。服务端把实例标识作为 `source.name`，把会话名（缺失时使用匿名 fallback）作为 title 和 `sessionName` 字段，并在有 `projectName` 时增加“项目”行。

Question/Permission 内容默认使用 `actionContentMode=strict`，只发送 Permission item 的 `category` 与 Question 的 `count`/`optionCount` 计数；`summary` 发送清洗、截断摘要而不发送完整描述；`full` 才会发送显式白名单中的问题文本、选项 label/description、推荐信息、权限标题/描述/操作目标或 patterns。Permission 使用 `{count, items[]}`，最多 16 个 item；Question 保持 `{count, optionCount, summary?, items?}`，最多 8 个问题、每题 12 个选项，计数可反映去重后的总量。`full` 仍受单段文本、数组和总 payload 上限约束。序列化后的 UTF-8 请求体超过 64 KiB 时，聚合体不会先发送原体或重试原体，而会降级为 Permission 的计数/类别摘要或 Question 的计数摘要；降级体仍超限则跳过发送。

聚合只使用 raw session ID 作为进程内内存 key，不出站、不写日志；成员用官方 request `id` 去重，缺失时仅使用外层 event id 作为本地 fallback。Permission 与 Question 始终分开聚合。

明确不会进入请求、日志或 `NormalizedEvent.raw` 的内容包括：

- 原始 session ID、完整本地路径和 cwd；
- `parentID` 值（只在 Client 内部用于判断 scope；诊断如开启也只记录 `parentIDState`，不发送、不存储、不渲染、不写入其值）；
- prompt、消息正文、工具输入输出、命令、diff 和 stack；
- strict 模式下 permission 的标题、描述、目标路径，以及 question 正文、选项正文；
- error message、response body 和任意未列入 allowlist 的字段；
- URL、Bearer Token、Token 文件内容。

Client 通过已有的 `input.client.session.get()` 判断 scope：非空字符串 `parentID` 为 `subagent`；只有原本会归类为 root/unknown 且清洗后的 Session 名称精确为 `smartfetch-secondary`（或显式配置的有限 auxiliary 名称）时才为 `auxiliary`，明确 `undefined/null` 仍为 `root`，API 失败、非对象、空字符串或类型异常为 `unknown`。OpenCode v1.18.4 的 `message.updated.properties.info` 仅在 `role=assistant` 时被消费；清洗后的 `mode` 作为 agent，`providerID/modelID` 作为 model 元数据，`info.time.created/completed` 仅在 idle 周期缺少可靠 busy 时间时作为任务时间 fallback，并按匿名 session ref 放入最多 1000 条、触发后保留最近 500 条的 LRU。若 agent/model 或 Assistant 元数据仍缺失，Client 最多调用一次 `session.messages(limit=10)`，逆序读取最后 assistant 的 `info`，不读取 `parts`。缓存不保存 raw ID、message ID、parts、路径、tokens 或 cost；新 busy 周期只清理上一周期的 Assistant 时间缓存，保留必要的 agent/model 元数据；unknown 不永久缓存，状态与缓存有界清理。若显式开启 `metadataDiagnostics`，诊断只记录这些读取动作的安全形状和 bounded allowlist 元数据，`parentID` 仍只记录 `parentIDState`，不记录其值；时间诊断只记录 `created/completed` 的存在性或 key 名。Assistant `info` 和 fallback `info` 仅可额外记录 `variant`、`reasoningEffort`、`reasoning_effort` 的清洗短 string/number/boolean；对象/数组只记录 `object`/`array` 类型。`session.get` 仅记录 nested model 的安全 `modelKeys`（最多 24 个）及对应的 `modelVariant`/`modelReasoningEffort`/`modelReasoning_effort`，顶层候选分别写为 `topLevelVariant`、`topLevelReasoningEffort`、`topLevelReasoning_effort`，不展开 model 或 provider options。

服务端对未知字段 fail-closed。不要把 OpenCode 原始 event object 直接 POST 到 AstrBot；必须由第一方 V1 Plugin 先转换为稳定 envelope。通知事件的丰富优先级为已有 event 字段、assistant 缓存、`session.get()` 兼容字段，最后才是一次 messages fallback；`provider/model`、provider-only 和 model-only 分别按组合、provider、model 展示，assistant `mode` 映射为 agent。`session.get` 或 messages 失败只记录固定脱敏 warning，不输出异常文本、ID、ref、标题或响应正文，且失败不阻断通知。若需要排查运行时结构，选择明确档位（例如低/高复杂度的新会话）运行多个新会话，临时设置 `metadataDiagnostics` 为 `once` 或 `sample`，采集后立即恢复为 `off`；诊断日志不得作为 payload、缓存或持久化数据使用。

---

## Timeout、retry 与 at-least-once

- `timeoutMs` 默认 10000 毫秒；配置为正数时使用配置值。
- 每个事件最多 3 次 HTTP 尝试：1 次初始请求加 2 次 retry。
- network error、timeout、HTTP 429 和 HTTP 5xx 可 retry；401、403、413 及其他 4xx 不 retry。
- backoff 使用指数退避和少量 jitter，默认从约 400 毫秒开始，单次上限 5000 毫秒；合法 `Retry-After` 会在上限内生效。
- Hook 对 OpenCode runtime 是 fire-and-forget，失败会记录脱敏分类并结束，不阻塞 OpenCode。

该链路是 **at-least-once 风格的尽力投递**，不是 exactly-once：网络超时发生在服务端已接收之后时，retry 可能产生重复请求。同一次客户端 retry 会保持稳定 `id`，但当前 AstrBot 服务端不提供幂等去重，重复请求可能产生重复通知。`skipped` 或非 retryable 4xx 不应继续重试。

---

## CLI smoke

在仓库根目录执行：

```bash
python scripts/smoke_opencode_plugin.py --cli
```

夹具会：

1. 创建临时 HOME、XDG config/data/state/cache 和临时 project；
2. 通过 `OPENCODE_CONFIG_CONTENT` 写入一份只包含 V1 plugin tuple 的配置；
3. 生成临时 wrapper，default export 仍是 `{id, server}`，并委托真实的 `integrations/opencode/webhook-notifier.ts`；
4. 用临时 env URL 和临时 token file 启动 `opencode serve --hostname 127.0.0.1`，并选择隔离的可用端口；
5. 请求隔离 project 的无模型 session-list API，初始化 Plugin Service，等待 wrapper 在 OpenCode 实际调用 `server(input, options)` 时写入 marker；
6. 超时或完成后只终止自己创建的进程组，并清理临时目录。

正常输出只包含版本、脱敏阶段和 `PASS`。没有 marker 时输出 `FAIL`，不能把“进程启动”伪造为 Plugin Service 已加载。退出码约定：`0` 为 PASS，`1` 为 FAIL，`2` 为 Desktop 安全 SKIP，`3` 为参数错误。

该 smoke 不读取用户 OpenCode config、auth 或 secrets，也不修改用户配置。它验证的是 Plugin Service 是否真正调用 V1 `server`，不是模型请求、真实平台消息或公网 HTTPS 验证。

---

## Desktop smoke

```bash
python scripts/smoke_opencode_plugin.py --desktop
```

脚本不会自动启动 Desktop，也不会连接、重载或终止已有 OpenCode Desktop。当前安全自动化边界只能返回 `SKIP`，并给出最小人工要求；`SKIP` 不能当作 PASS。

执行本节前必须先完成测试 ZIP 安装、AstrBot 插件重载、OpenCode Endpoint 创建和 URL/Token 配置。仅重启 Desktop 而服务端仍是旧版本，不能构成有效 smoke。

如需人工验证：

1. 创建全新的隔离 Desktop profile/instance，不要复用正在运行的 profile。
2. 在该隔离实例中使用脱敏的 V1 plugin tuple 和 env/file 凭据配置。
3. 确认 Plugin Service 已加载，再触发不需要模型输出的 session 状态或 permission 事件。
4. 只核对事件类别、HTTP 状态和脱敏日志；结束后仅关闭这个新建实例。

如果无法保证“新 profile、新进程、只关闭自己启动的实例”，应保留 `SKIP`，不要强行自动化。

---

## 排障

| 现象 | 检查项 | 安全处理 |
| --- | --- | --- |
| smoke 输出 `resolve-binary` | CLI 是否可执行，或显式传入 `--opencode-bin` | 不要改用户配置；只提供本次命令的 executable |
| smoke 输出 `plugin-server-timeout` | `plugin` 是否为二元 tuple、模块 URL 是否可读、wrapper 是否是 default `{id,server}` | 查看隔离 CLI 的脱敏 stderr；不要打印完整配置或 Token |
| Plugin 被加载但没有请求 | URL/env/file 插值、Endpoint Path、Bearer Token、事件过滤器 | 先用固定脱敏事件验证，不要把 Token 放到 URL |
| HTTP 401/403 | Endpoint provider、Token 是否属于同一条 OpenCode Endpoint | 重新从 AstrBot 私聊 rotate，旧 Token 不恢复 |
| HTTP 413 或未知字段错误 | payload 是否绕过了官方 Plugin、是否携带 raw/cwd/prompt | 回到 V1 allowlist，不要放宽服务端白名单 |
| 只收到 idle 或重复 idle | 是否先收到 busy、是否同一周期多次 idle、是否发生 retry | 以 `id` 去重；检查状态机和 at-least-once 语义 |
| 新 Client 返回未知字段 | 是否先升级服务端 | 服务端必须先升级并重载，再重启 OpenCode Client；旧严格 allowlist 服务端会拒绝 `session.scope` |
| Desktop 无法安全验证 | 正在运行的实例或 profile 无法隔离 | 保持 `SKIP`，按上面的新隔离实例步骤人工验证 |

相关文档：[公共契约](public-contract.md)、[命令参考](command-reference.md)、[安全与运维](security-and-operations.md)。
