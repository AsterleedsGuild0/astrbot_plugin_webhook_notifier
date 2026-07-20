# OMP 客户端社区 Hook 集成指南

## 文档信息

- 适用插件版本：v0.3.0 及后续保持兼容的版本
- 验证日期：2026-07-20
- 集成性质：外部社区依赖
- 社区实现：`ParticleG/omp-config` 的 `agent/hooks/post/onebot.ts`

本文说明如何把 OMP 会话结束事件通过社区 Hook 发送到 Webhook Notifier。该 HTTP POST 实现不属于 OMP 内建 Webhook 功能；外部仓库独立维护且未见明确 License，本文只提供链接和配置说明，不复制或分发其源代码。使用前请自行确认上游许可、版本兼容性与组织安全要求。

首次从 AstrBot 服务端、HTTPS 反向代理一路部署到真实 OMP 会话时，请先阅读[端到端部署教程](end-to-end-setup.md)。本文只负责 OMP 侧社区 Hook 的获取、环境变量、HTTP 契约与数据边界；插件服务端配置、Endpoint 创建和 Caddy 参考部署以端到端教程为准。

---

## OMP 原生机制与社区实现的边界

OMP 原生提供 extension、Hook 加载机制，以及 `session_stop` 生命周期事件。HTTP POST、环境变量读取和 version 1 JSON payload 由 [`ParticleG/omp-config` 的 `onebot.ts`](https://github.com/ParticleG/omp-config/blob/main/agent/hooks/post/onebot.ts) 社区 Hook 实现，不是 OMP 原生 Webhook。

本插件的 `omp` provider 名称为既有服务端兼容标识。它当前兼容并标准化上述社区 Hook 输出的 `omp.session_stop` version 1 payload，不表示 OMP 官方承诺该 HTTP 契约。

---

## 前提与获取方式

- 已部署可加载 extension/Hook 的 OMP 环境。
- 已在 Webhook Notifier 中创建 Endpoint，并分别取得 Base URL、Endpoint Path 和 Bearer Token。
- 从上游仓库直接获取或引用 [`agent/hooks/post/onebot.ts`](https://github.com/ParticleG/omp-config/blob/main/agent/hooks/post/onebot.ts)；完整项目见 [`ParticleG/omp-config`](https://github.com/ParticleG/omp-config)。
- 不要从本文复制 Hook 源码，也不要把上游源码重新打包到本插件。

OMP 自带 Bun/TypeScript 运行能力，部署该 Hook 不需要执行 `npm install`、`bun install` 或手动编译。本文不声明未经验证的最低 OMP 版本。

---

## 在 OMP 中安装 onebot.ts

### 确认来源与使用授权

社区脚本来源为 [`ParticleG/omp-config` 的 `agent/hooks/post/onebot.ts`](https://github.com/ParticleG/omp-config/blob/main/agent/hooks/post/onebot.ts)。该仓库当前未见明确 License；从上游取得脚本前，应确认自己有权使用，并记录取得日期和对应上游 commit。

本仓库不分发该脚本，也不复制其完整源码。本文只说明如何安装已经通过组织授权流程取得的本地 `onebot.ts` 文件，不提供 raw 下载命令。

### 选择自动发现目录

推荐使用 OMP 自动发现，无需额外 settings 注册。二选一安装：

- 用户级：`~/.omp/agent/hooks/post/*.ts`，对该用户启动的 OMP 生效。
- 项目级：`.omp/hooks/post/*.ts`，对当前项目生效；这里是项目根目录下的点目录 `.omp`，不是 `./omp`。

用户级安装已有本地文件：

```bash
mkdir -p ~/.omp/agent/hooks/post
install -m 600 /path/to/onebot.ts ~/.omp/agent/hooks/post/onebot.ts
```

项目级替代方案：

```bash
mkdir -p .omp/hooks/post
install -m 600 /path/to/onebot.ts .omp/hooks/post/onebot.ts
```

OMP 只发现 `post` 目录中的直接子文件，不递归扫描其子目录。文件名不要求固定，但模块必须默认导出工厂函数；保留 `onebot.ts` 文件名便于与上游对照。不要同时在用户级、项目级或显式配置中放置同一 Hook 的多份副本，否则可能重复推送。

### 配置启动进程环境变量

在受控环境中组合 Base URL 与 Endpoint Path，并把以下变量放入启动 OMP 的进程环境：

```bash
export OMP_SESSION_WEBHOOK_URL='<Base URL与Endpoint Path组合后的完整Endpoint URL>'
export OMP_SESSION_WEBHOOK_TOKEN='<创建Endpoint时单独返回的Bearer Token>'
export OMP_SESSION_WEBHOOK_TIMEOUT_MS='5000'
```

可以把变量配置在 shell 启动配置、受控包装启动脚本或服务管理器的环境配置中。环境变量必须实际传入启动 OMP 的进程；设置后需要重启 OMP。当前没有上游确认的 OMP 自动加载 `.env` 约定，不要仅创建 `.env` 后假设变量已经生效。

社区脚本在 URL 未配置时会静默不发送。虽然脚本本身可以在 Token 为空时发出无鉴权请求，但本插件 Endpoint 强制要求 Bearer Token，因此必须使用创建 Endpoint 时单独返回的 Token，并与 URL 分开保存。

### 重启并验证自动发现

重启 OMP 后执行：

```bash
omp -p '/extensions'
```

检查输出的已加载扩展路径是否包含所安装的 `onebot.ts`。加载失败时检查 `~/.omp/logs/` 中的 OMP 日志，并确认文件位于直接子目录、权限可读、模块具有默认工厂导出。

不同 OMP 版本若没有自动加载，可先升级 OMP，再使用显式注册或 `--hook` 进行诊断。

### 必要时显式注册或一次性诊断

可以在用户级 `~/.omp/agent/settings.json` 或项目级 `.omp/settings.json` 的 `extensions` 数组中显式注册本地文件。以下只是结构片段，必须合并到已有 `extensions` 数组，不要覆盖用户现有 settings 或其他扩展：

```json
{
  "extensions": [
    "/absolute/path/to/onebot.ts"
  ]
}
```

也可以一次性启动并加载 Hook，用于区分自动发现与脚本本身的问题：

```bash
omp --hook /path/to/onebot.ts
```

显式注册和 `--hook` 是兼容或诊断手段；正常部署优先使用用户级或项目级自动发现目录。

### 触发并分层验证

1. 先使用 [`README` 的 curl 示例](../README.md)确认插件、Endpoint、Token 和 AstrBot 投递链路。
2. 启动已加载 Hook 的 OMP，完成一次真实的主代理对话，并让主代理正常完成一轮响应；这会触发 `session_stop`。任务或子代理会话不会触发该事件。
3. 若没有请求，先用 `omp -p '/extensions'` 确认加载路径，再确认 URL 环境变量确实进入 OMP 进程。URL 未配置时脚本会静默跳过。
4. 若发送失败，社区脚本会通过 `pi.logger.warn("Failed to send OMP session webhook", ...)` 记录警告；在 `~/.omp/logs/` 检查脱敏后的错误类型、网络和 timeout。
5. 再检查反向代理访问日志、AstrBot 脱敏日志和 Bot 目标是否送达，不要公开完整 URL、Endpoint Path、Token 或 payload。

### 更新策略

记录脚本取得日期和实际采用的上游 commit，不要让生产环境自动追踪可变的 `main`。更新前后检查本地文件与目标上游版本的 diff，重点复核新增外发字段、环境变量、日志内容和网络行为；更新后重新执行扩展加载检查、curl 模拟和真实主代理轮次验证。

---

## 配置步骤

1. 在已认证的 AstrBot Plugin Page 获取 Base URL。
2. 在与 Bot 的聊天中创建或查看 Endpoint，取得 Endpoint Path；Token 仅在创建、轮换或确认成功时独立交付。
3. 在受控环境中组合 Base URL 与 Endpoint Path，避免在聊天、Issue、截图或 shell history 中公开完整 URL。
4. 将完整 URL 配置到上游 Hook 的 URL 环境变量，将 Token 配置到独立 Token 环境变量。
5. 如需调整请求超时，设置毫秒值；未设置时上游默认约为 5 秒。
6. 按 OMP 和 `omp-config` 的部署方式加载 Hook，再触发验证。

安全占位示例：

```bash
export OMP_SESSION_WEBHOOK_URL='<受控环境中组合的完整URL>'
export OMP_SESSION_WEBHOOK_TOKEN='<独立保存的Bearer Token>'
export OMP_SESSION_WEBHOOK_TIMEOUT_MS='5000'
```

不要把 Token 拼入 URL，也不要在公开材料中替换占位符为真实值。

---

## 环境变量

| 主变量 | fallback | 用途 |
| --- | --- | --- |
| `OMP_SESSION_WEBHOOK_URL` | `ONEBOT_WEBHOOK_URL` | Webhook Notifier 的完整 Endpoint URL |
| `OMP_SESSION_WEBHOOK_TOKEN` | `ONEBOT_WEBHOOK_TOKEN` | 创建 Endpoint 时单独返回的 Bearer Token；本插件鉴权必需 |
| `OMP_SESSION_WEBHOOK_TIMEOUT_MS` | `ONEBOT_WEBHOOK_TIMEOUT_MS` | HTTP 请求超时，单位为毫秒；默认约 5 秒 |

优先使用 `OMP_SESSION_*` 主变量。fallback 用于兼容上游既有配置，不应同时配置相互冲突的值。

---

## 生命周期与 HTTP 契约

社区 Hook 订阅 OMP 原生 `session_stop` 生命周期事件，并执行一次 HTTP POST：

- 方法：`POST`
- Body：JSON
- `Content-Type`：`application/json`
- 事件 Header：`X-OMP-Event: session_stop`
- 鉴权 Header：配置 Token 时发送 `Authorization: Bearer <TOKEN>`
- 超时：默认约 5 秒，可通过环境变量调整
- 失败策略：记录 warn，不重试，不阻断 OMP 的会话结束流程

`onebot.ts` 是上游文件名，不表示本插件要求客户端直接调用 OneBot 协议。客户端只需向本插件 HTTP Endpoint 发送符合契约的 JSON；后续 AstrBot 消息投递由插件处理。

### 服务端兼容字段

| 区域 | 可能字段 | 服务端用途 |
| --- | --- | --- |
| 顶层 | `event`、`version`、`emittedAt` | 识别 `omp.session_stop` version 1 和事件时间 |
| `session` | `id`、`file`、`cwd`、`name`、`model` | 生成事件 ID、上下文与模型展示 |
| `round` | `turnId`、`startedAt`、`endedAt`、`durationMs` | 轮次标识与耗时 |
| `round` | `prompt`、`promptLength`、`imageCount` | 输入规模；prompt 由上游截断至约 2000 字符 |
| `round` | entry/message 的 before、after、delta 计数 | 轮次变化统计 |
| `round.lastAssistant` | provider、model、stopReason、timestamp、durationMs 等 | 最后 assistant 状态和模型回退 |
| `metadata` | `version`、`eventName` | 兼容诊断与 raw 保留 |

字段可能缺失；本插件按可选字段进行标准化。Header 与 Body 事件同时存在但不一致时，请求会被拒绝。

---

## 数据与隐私

社区 Hook 可能外发：

- session id、名称、模型；
- session file 和工作目录 `cwd`；
- round turnId、开始/结束时间、耗时；
- prompt、prompt 长度和图片数量；
- entry/message 计数；
- lastAssistant 元数据。

其中 prompt、`cwd`、session file 可能包含业务内容、用户输入、仓库结构、用户名或本地路径。即使默认聊天通知不展示完整 prompt 或 session file，原始 HTTP payload 仍可能携带这些字段，使用方必须在启用前评估数据最小化、跨网络传输、日志留存和接收目标权限。

根据当前上游实现，失败日志可能记录目标 URL，但不应记录 Token 或完整 payload。仍应避免把敏感信息放入 URL，并检查组织的日志采集与访问控制。

---

## 验证步骤

1. 先使用 [`README` 的 curl 示例](../README.md)模拟 version 1 请求，确认 URL、Token、Endpoint 和服务端投递链路。
2. 再完成一次真实主代理响应以触发 `session_stop`；任务或子代理会话不触发该事件。
3. 检查 HTTP JSON 结果：
   - `message=ok`：至少一个目标成功投递。
   - `message=skipped`：请求已处理，但目标被安全策略跳过；常见于私聊通知默认关闭，不应重试。
   - `message=partial_delivery`：部分目标成功，部分目标被跳过或失败；查看逐目标结果和 `retryable`。
   - 非 2xx 或鉴权错误：先检查 URL、Token 和 Endpoint 状态。
4. 同时检查 `omp -p '/extensions'`、`~/.omp/logs/` 中的 warn 与 AstrBot 侧脱敏日志，但不要公开完整 URL、Token 或 payload。

---

## 排障

### URL 拼接错误

确认 Base URL 已包含部署所需的基础路径，组合时只追加聊天返回的 Endpoint Path，避免重复或遗漏 `/webhook`，并检查多余斜杠。

### Token 或鉴权失败

Token 必须放在独立环境变量中。轮换后旧 Token 立即失效；不要从 Registry、日志或历史聊天中恢复旧明文。

### 私聊默认关闭

私聊 Endpoint 可以完成鉴权，但 `enable_private_notifications=false` 时返回 HTTP 200 `skipped`。确认平台规则与风险后再由管理员显式开启。

### 网络或 timeout

确认 OMP 运行环境可访问插件 Endpoint、TLS 证书有效、反向代理允许 POST，并根据受控网络延迟调整 `OMP_SESSION_WEBHOOK_TIMEOUT_MS`。上游失败不重试，临时网络故障可能导致单次通知丢失。

### payload 变更

确认 `event=omp.session_stop`、`version=1`，并检查 Header 与 Body 是否一致。字段缺失通常可兼容，事件名、版本或结构变化可能需要插件升级。

### 上游升级

`main` 分支文件可随时变化。升级 `omp-config` 或 OMP 后，重新核对环境变量、Hook 加载目录、payload、日志行为和许可状态，并执行 curl 与真实会话双重验证。

---

## 兼容性与责任边界

- OMP 原生负责 extension/Hook 加载和 `session_stop` 生命周期事件。
- `ParticleG/omp-config` 独立负责 HTTP POST 社区 Hook、环境变量和 version 1 payload。
- Webhook Notifier 负责接收、鉴权、兼容解析、标准化、渲染和 AstrBot 投递。
- 外部项目独立维护，本插件不分发其代码，也不保证未来上游变化继续兼容。
- 上游 `main` 链接不是固定发布物；生产环境应记录实际采用的上游 commit，并在升级前复验。
- 外部仓库未见明确 License。部署或再分发前，使用方应自行确认上游许可；本指南不构成许可意见。

---

## 相关链接

- [端到端部署教程](end-to-end-setup.md)
- [项目 README](../README.md)
- [安全与运维指南](security-and-operations.md)
- [`ParticleG/omp-config` 上游源码](https://github.com/ParticleG/omp-config/blob/main/agent/hooks/post/onebot.ts)
- [OMP Hooks 文档](https://omp.sh/docs/hooks)
- [OMP 官方 Extension Loading 文档](https://github.com/can1357/oh-my-pi/blob/main/docs/extension-loading.md)
- [OMP 官方 Hooks 文档](https://github.com/can1357/oh-my-pi/blob/main/docs/hooks.md)
