# OpenCode 集成指南

本文说明 OpenCode CLI/Desktop Plugin 与 Webhook Notifier 的 V1 接入边界。文中的路径、URL、Token 和环境变量值均为占位符；不要把真实部署值复制到公开文档、Issue、日志或截图中。

## 版本基线与验证目标

- OpenCode Plugin API 基线：`v1.17.9`。
- 当前实际本机 CLI/Desktop 验证目标：`v1.18.4`。
- 集成入口是 OpenCode V1 file Plugin 的 `default { id, server }` 形态，不是旧版 `tui`、裸函数或任意自定义导出。
- Python 服务端使用 Endpoint 的 Bearer 鉴权和 `opencode` provider；创建后 provider 不可变。

`v1.17.9` 是本插件 TypeScript 类型和调用约定的 API 基线，`v1.18.4` 是本机 smoke 使用的实际运行时版本。升级 OpenCode 后应重新执行 CLI smoke，并观察事件、配置插值和 Plugin Service 加载行为。

---

## 完整端到端验收顺序

OpenCode Desktop 测试依赖已经部署的新 AstrBot 服务端和一条真实的 `provider=opencode` Endpoint。不能只把 TypeScript Plugin 放进 OpenCode 后重启，也不能把 CLI loader smoke 当作完整联调结果。

按以下顺序执行：

1. 将 Provider Registry、OpenCode Server Adapter、OpenCode Client Plugin、测试和文档修改提交到可追溯 commit。
2. 使用新版本号构建 AstrBot 插件测试 ZIP，校验 ZIP 根目录、版本字段、文件清单和 checksum。
3. 在测试 AstrBot 中安装该 ZIP 并重载 Webhook Notifier，确认运行版本已经更新。
4. 与 Bot 交互创建 `provider=opencode` Endpoint，从认证 Plugin Page 获取 Base URL，并分别取得 Endpoint Path 与一次性交付的 Bearer Token。
5. 在运行 OpenCode 的机器部署 `webhook-notifier.ts`，把组合后的完整 Endpoint URL 与 Token 通过 env/file 插值交给 Plugin。
6. 完全退出并重新启动 OpenCode Desktop 或 CLI，使 file Plugin 和配置进入新进程。
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
          "permission_asked"
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

`server` 返回包含 `event` hook 的 hooks 对象。插件只使用 OpenCode 提供的 `input.client.session.get()` 做非关键的会话标题、agent 和 model 丰富；丰富失败不阻止事件发送。

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

## 三事件与状态机

插件监听 OpenCode runtime event，向 AstrBot 发送固定 V1 envelope。服务端要求 `X-OpenCode-Event` 与 body 的 `event` 一致。

| OpenCode 输入 | Webhook 事件 | 处理方式 |
| --- | --- | --- |
| `session.status` 从 `busy` 到 `idle`，或兼容的 `session.idle` | `opencode.session_idle` | 当前工作周期只发送一次 |
| `session.error` | `opencode.session_error` | 立即发送，并抑制当前周期后续 idle |
| `permission.updated`，兼容 `permission.asked` | `opencode.permission_asked` | 独立发送，不改变 busy/idle 周期 |

状态规则：

1. 初始 `idle` 没有先前 `busy` 时忽略，避免启动时重复通知。
2. `busy` 开始新周期，清除上一周期的 idle/error 抑制状态。
3. 一个周期的 `error` 已通知后，后续 `idle` 不再补发完成通知。
4. 同一周期的 `idle`、兼容旧 `session.idle` 和并发 idle 只允许一个通知通过。
5. permission 事件不依赖 busy/idle 状态，可在任意周期独立通知。

---

## 匿名 ref 与 name fallback

OpenCode 原始 session ID 不离开 Plugin。Plugin 计算：

```text
session.ref = first_32_hex(SHA-256("opencode:" + raw_session_id))
```

服务端的 `NormalizedEvent` 只保留安全的短 ref 展示值，不保留原始 session ID。会话名称经过以下处理：

- 删除 bidi、zero-width、format 和行/段分隔控制字符；
- 将控制字符、换行和连续空白归一化；
- 最多保留 200 个 Unicode 字符；
- 保留普通 Unicode、CJK、emoji 和普通 HTML/Markdown 字符，最终转义由服务端 renderer 负责。

如果 `session.name`、`session.title` 都不可用或清洗后为空，通知标题回退为：

```text
OpenCode Session <ref12>
```

其中 `<ref12>` 只从 ref 的 ASCII 字母、数字、`.`、`_`、`-` 构建，最多 12 个字符。该 fallback 是可读的匿名关联标识，不是原始 session ID。

---

## 白名单与隐私边界

Envelope 只允许以下顶层字段：`id`、`event`、`version`、`emittedAt`、`session`、`agent`、`model`、`durationMs`、`permission`、`error`。`session` 只允许 `ref` 与清洗后的 `name`；事件专属对象只允许安全的 category/code。

明确不会进入请求、日志或 `NormalizedEvent.raw` 的内容包括：

- 原始 session ID、完整本地路径和 cwd；
- prompt、消息正文、工具输入输出、命令、diff 和 stack；
- permission 的标题、描述、目标路径；
- error message、response body 和任意未列入 allowlist 的字段；
- URL、Bearer Token、Token 文件内容。

服务端对未知字段 fail-closed。不要把 OpenCode 原始 event object 直接 POST 到 AstrBot；必须由第一方 V1 Plugin 先转换为稳定 envelope。

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
| Desktop 无法安全验证 | 正在运行的实例或 profile 无法隔离 | 保持 `SKIP`，按上面的新隔离实例步骤人工验证 |

相关文档：[公共契约](public-contract.md)、[命令参考](command-reference.md)、[安全与运维](security-and-operations.md)。
