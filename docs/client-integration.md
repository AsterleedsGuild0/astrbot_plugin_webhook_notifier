# OMP 客户端 Hook 集成指南

## 文档信息

- 服务端协议：新增 `omp.run_finished` / `omp.action_required` version 2，保留 V1 接收
- 客户端维护入口：[`integrations/omp/webhook-notifier.ts`](../integrations/omp/webhook-notifier.ts)
- 当前 API 研究及本机版本：OMP `18.12.6`，2026-10-23
- 发布状态：V2 本地开发版，尚未部署；已发布的 v2.0.0 ZIP 不含此客户端

本文说明如何将 OMP 运行收束和交互观察发送到 Webhook Notifier。维护源只发送 V2，不自动降级。历史 V1 基线来自 [`ParticleG/omp-config`](https://github.com/ParticleG/omp-config/blob/main/agent/hooks/post/onebot.ts)；V1 接收兼容和状态纠错见[公共契约](public-contract.md#v1-接收兼容与纠错)。

macOS 开发环境可以通过[本地 Hook 自动同步](#本地-hook-自动同步)部署普通文件副本。创建仓库源文件不等于接管本机已加载的 Hook；切换文件名和启动同步需单独执行。

首次从 AstrBot 服务端、HTTPS 反向代理一路部署到真实 OMP 会话时，请先阅读[端到端部署教程](end-to-end-setup.md)。本文负责客户端安装、环境变量、HTTP 契约与数据边界；插件服务端配置、Endpoint 创建和 Caddy 参考部署以端到端教程为准。

---

## OMP 原生机制与客户端的边界

OMP 原生提供 ExtensionAPI 事件和加载机制，HTTP 协议由本仓库维护。`session_stop` 可能被其他扩展要求续跑，并不证明最终成功；Hook 等待维护路由后的 `agent_end`，结合 `willContinue`、重试结果和结构化 assistant 状态判断。子代理不依赖 `session_stop`。

V2 使用公开代理身份设置 `root|subagent|unknown`；服务端只过滤规范 `completed` 的成功短任务。审批、计划审阅和 ask 只观察公开事件，不修改工具输入／结果、不返回阻断值。源码依据不等于原生 OMP 场景已验收。

---

## 前提与获取方式

- 已部署可加载 extension/Hook 的 OMP 环境。
- 已显式创建 OMP Endpoint，例如 `<唤醒词>whn token new private my-omp --provider omp`，并分别取得 Base URL、Endpoint Path 和 Bearer Token。群 Endpoint 同样追加 `--provider omp`，按[命令参考](command-reference.md)完成验证。自 `v2.0.0` 起省略此 flag 默认 `markdown`，不能发送本指南的 OMP payload；错误 flag 不回退默认值。旧记录缺失 `provider` 也按 Markdown 读取，显式历史 Provider 保留、不自动覆盖、不按 payload 猜协议；依赖缺失字段的旧 OMP 调用应创建新的显式 OMP Endpoint 并更新 URL/Token。发布边界见[公共契约](public-contract.md)。
- 从本仓库取得 `integrations/omp/webhook-notifier.ts`，或从包含该文件的后续发布 ZIP 中提取；无需克隆外部社区配置仓库。
- 确认现有 Hook 入口，切换时避免旧 `onebot.ts` 与新客户端重复加载。

OMP 自带 Bun/TypeScript 运行能力，部署该 Hook 不需要执行 `npm install`、`bun install` 或手动编译。本文不声明未经验证的最低 OMP 版本。

---

## 在 OMP 中安装 webhook-notifier.ts

### 确认维护源与旧入口

本仓库的 `integrations/omp/webhook-notifier.ts` 是开发维护源，OMP 加载目录中的文件是部署副本。部署时记录插件仓库 commit 或发布版本。

已有 `onebot.ts` 时，先将它备份到 OMP 自动发现目录之外，再安装新文件；不要把备份保留为 `post` 目录中的另一个 `.ts`。本轮入库不自动移除或覆盖既有入口。

### 选择自动发现目录

推荐使用 OMP 自动发现，无需额外 settings 注册。二选一安装：

- 用户级：`~/.omp/agent/hooks/post/*.ts`，对该用户启动的 OMP 生效。
- 项目级：`.omp/hooks/post/*.ts`，对当前项目生效；这里是项目根目录下的点目录 `.omp`，不是 `./omp`。

在插件仓库根目录执行用户级安装：

```bash
mkdir -p ~/.omp/agent/hooks/post
install -m 600 integrations/omp/webhook-notifier.ts ~/.omp/agent/hooks/post/webhook-notifier.ts
```

项目级替代方案，在需要启用 Hook 的项目根目录执行：

```bash
mkdir -p .omp/hooks/post
install -m 600 /path/to/astrbot_plugin_webhook_notifier/integrations/omp/webhook-notifier.ts .omp/hooks/post/webhook-notifier.ts
```

OMP 只发现 `post` 目录中的直接子文件，不递归扫描其子目录。文件名不要求固定，但模块必须默认导出工厂函数；本仓库使用 `webhook-notifier.ts`，因为它发送插件 Webhook，并不直接调用 OneBot。不要同时在用户级、项目级或显式配置中放置同一 Hook 的多份副本，否则可能重复推送。

### 配置启动进程环境变量

在受控环境中组合 Base URL 与 Endpoint Path，并把以下变量放入启动 OMP 的进程环境：

```bash
export OMP_SESSION_WEBHOOK_URL='<Base URL与Endpoint Path组合后的完整Endpoint URL>'
export OMP_SESSION_WEBHOOK_TOKEN='<创建Endpoint时单独返回的Bearer Token>'
export OMP_SESSION_WEBHOOK_TIMEOUT_MS='5000'
```

可以把变量配置在 shell 启动配置、受控包装启动脚本或服务管理器的环境配置中。环境变量必须实际传入启动 OMP 的进程；设置后需要重启 OMP。当前没有上游确认的 OMP 自动加载 `.env` 约定，不要仅创建 `.env` 后假设变量已经生效。

Hook 在 URL 未配置时会静默不发送。虽然客户端本身可以在 Token 为空时发出无鉴权请求，但本插件 Endpoint 强制要求 Bearer Token，因此必须使用创建 Endpoint 时单独返回的 Token，并与 URL 分开保存。

### 重启并验证自动发现

重启 OMP 后执行：

```bash
omp -p '/extensions'
```

检查输出的已加载扩展路径是否包含所安装的 `webhook-notifier.ts`。加载失败时检查 `~/.omp/logs/` 中的 OMP 日志，并确认文件位于直接子目录、权限可读、模块具有默认工厂导出。

不同 OMP 版本若没有自动加载，可先升级 OMP，再使用显式注册或 `--hook` 进行诊断。

### 必要时显式注册或一次性诊断

可以在用户级 `~/.omp/agent/settings.json` 或项目级 `.omp/settings.json` 的 `extensions` 数组中显式注册本地文件。以下只是结构片段，必须合并到已有 `extensions` 数组，不要覆盖用户现有 settings 或其他扩展：

```json
{
  "extensions": [
    "/absolute/path/to/webhook-notifier.ts"
  ]
}
```

也可以一次性启动并加载 Hook，用于区分自动发现与脚本本身的问题：

```bash
omp --hook /path/to/webhook-notifier.ts
```

显式注册和 `--hook` 是兼容或诊断手段；正常部署优先使用用户级或项目级自动发现目录。

### 触发并分层验证

1. 先在独立配置与回环测试目标执行下方本地 smoke；不得仅因默认同步器暂停就启动带真实环境变量的 OMP。
2. 获得相应授权后，用隔离 HOME、独立扩展入口与测试凭据执行原生场景。当前 Hook 在最终 `agent_end` 收束时发送；`willContinue=true` 的重试、维护和后台等待不提前完成。
3. 只有 URL、Token 同时有效才发送；缺失／非法时记录固定配置分类。客户端日志 `OMP webhook` 只含分类、HTTP 状态、尝试次数和匿名事件引用。
4. 同时检查 HTTP 和受限业务结果；`delivered` 表示服务端报告送达，`replayed` 不能作为实际消息证明。线上还需核对测试目标消息。

### 更新策略

先升级支持 V2 的服务端，再单独授权切换客户端。保留单一生效入口；不恢复旧 `onebot.ts` 为第二入口。默认同步器在开发期间保持停止／禁用，不覆盖保留的真实部署副本。旧端拒绝独立 V2 事件，不自动回退。测试包、切换门禁及同入口客户端回滚步骤见[发布流程](release.md#omp-v2-测试包与切换门禁未发布)。

---

## 配置步骤

1. 在已认证的 AstrBot Plugin Page 获取 Base URL。
2. 在与 Bot 的聊天中创建或查看 Endpoint，取得 Endpoint Path；Token 仅在创建、轮换或确认成功时独立交付。
3. 在受控环境中组合 Base URL 与 Endpoint Path，避免在聊天、Issue、截图或 shell history 中公开完整 URL。
4. 将完整 URL 配置到 Hook 的 URL 环境变量，将 Token 配置到独立 Token 环境变量。
5. 如需调整请求超时，设置毫秒值；未设置时默认 5 秒。
6. 按本文安装并加载 Hook，再触发验证。

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
| `OMP_SESSION_WEBHOOK_TIMEOUT_MS` | `ONEBOT_WEBHOOK_TIMEOUT_MS` | 100–30000 的整数毫秒，默认 5000 |
| `OMP_SESSION_WEBHOOK_INSTANCE` | 无 | 显式实例别名，清洗控制字符并限制 96 字符 |

主变量优先；`ONEBOT_*` 仅保留已承诺的配置兼容。URL、Token 取首个非空值，URL 必须为 http(s)，不得含用户信息、查询或 fragment；Token 不得含换行。timeout 取首个有效值，否则使用默认值。凭据不读文件、不写日志。

---

## 生命周期与 HTTP 契约

完整字段、状态、大小、匿名引用、运行／交互边界和重试语义以[OMP V2 公共契约](public-contract.md#omp-v2运行收束与交互观察未发布)为准。

- 新客户端只发送 `omp.run_finished`、`omp.action_required`，整数 `version=2`，Header 与 body 事件一致。
- `completed` 是运行正常结束，不证明业务验收成功；最终失败、中止、待处理和未知分别表达。
- 请求去重与解决撤销在客户端有界内存中进行；150ms 内解决不产生过期提醒，队列发送和重试前再次检查。ask 是调用观察，审批不证明已呈现 UI，计划审阅区间可能包含处理耗时。
- 观察 handler 快速返回，不 await 网络；发送并发 2、队列 64、最多 3 次尝试，重试 ID 不变。退出不阻塞。
- HTTP 401/403 不重试；429/5xx／网络失败有界重试。策略跳过、部分失败和幂等重放不换 ID 重发；HTTP 200 不自动视为送达。
- 默认卡片复用现有文本／HTML 链路；时间统一按全局 `display_timezone` 展示，不改变传输时间。
- OMP 根通知使用共用时间线，但保留观察语义：审批、计划审阅、ask、后台等待分轨，worker 的周期／续跑段分开。显示已观测覆盖并集，缺口不显示为精确零；未分类时间不等于主代理执行时间。
- 实际模型／thinking 来自公开元数据边界采样，观察到变化后省略单一值。附图失败保留主卡；主卡图片失败按已有配置回退文本，不读取 raw 补正文。

## 数据与隐私

只发送有界实例／会话／代理名称、cwd basename、匿名引用、结构化状态、时间及公开模型／实际 thinking。名称不从 prompt、问题或回答截取；路径形态名称舍弃。名称本身仍可能包含业务信息。

禁止出站或日志出现正文、完整路径、原始 ID、工具参数、计划、原始错误和原始对象。OMP Sender 只记录固定失败分类，不保留异常正文与 traceback。V1 继续接收，但标准化不再透出旧完整路径、raw 元数据或原始 ID；`error`／`aborted` 纠正，普通 `stop` 缺最终证据时为 `unknown`。

## 本地验证与层级

在仓库根目录执行：

```bash
bun test integrations/omp integrations/opencode
uv run pytest -q
uv run ruff check .
uv run python scripts/smoke_omp_hook.py
```

smoke 在已有 `tmp/` 下创建临时 HOME、脚本与 Registry，退出清除。驱动维护源 Hook，经真实回环 HTTP、真实 Endpoint 鉴权、Adapter、策略、文本及 Sender，到内存消息接收器；不是线上 AstrBot。`--hook <测试ZIP提取入口>` 可验证打包副本，不默认使用真实部署文件。

本地行为回归、HTTP、原生 OMP 和线上投递必须分别留证，详见[验收记录](omp-v2-acceptance.md)。历史 2026-10-22 的 293 项 Bun／1226 项 Python 和旧 ZIP smoke 仅属于 V1 维护基线，不证明 V2。

---

## 排障

### URL 拼接错误

确认 Base URL 已包含部署所需的基础路径，组合时只追加聊天返回的 Endpoint Path，避免重复或遗漏 `/webhook`，并检查多余斜杠。

### Token 或鉴权失败

Token 必须放在独立环境变量中。轮换后旧 Token 立即失效；不要从 Registry、日志或历史聊天中恢复旧明文。

### 私聊默认关闭

私聊 Endpoint 可以完成鉴权，但 `enable_private_notifications=false` 时返回 HTTP 200 `skipped`。确认平台规则与风险后再由管理员显式开启。

### 网络或 timeout

确认受控目标网络和 TLS，timeout 配置范围为 100–30000ms。最多 3 次有界尝试，超时属于结果未知，不能推断服务端未发送；重放结果也不证明送达。

### payload 变更

新版必须使用 V2 独立事件名与整数 version 2；错误版本、未知字段、超限结构拒绝。V1 调用不自动升级，具体兼容值与状态纠错见公共契约。

### Hook 或 OMP 升级

升级本仓库 Hook 或 OMP 后，重新核对环境变量、Hook 加载目录、payload 和日志行为。文件同步成功不证明 OMP 生命周期兼容，也不证明 AstrBot 已投递。

---

## 兼容性与责任边界

- OMP 原生负责事件 producer 和扩展绑定；Hook 不改造上游调度。
- 本仓库维护源负责 V2 白名单、观察状态、匿名关联和有界 HTTP 投递。
- Webhook Notifier 负责鉴权、V1/V2 解析、规范策略、共用渲染和 AstrBot 投递。
- 各层验收独立；未验证原生或线上场景不得记为通过。
- 主通知汇总已加载实例的观察时间线；正常子代理完成不逐条发送，异常／需处理不因此静默。多个根实例仅在 parent 身份与快照证据唯一时关联；无法证明时明确不完整。段、后台等待和交互各保留原义，不代表完整调度或纯人工等待；字段与上限见[公共契约](public-contract.md#多代理与交互观察时间线)。
- OMP 原生 API 后续变化仍需独立验证；历史社区源码链接用于来源追溯，不是自动更新源。

---

## 本地 Hook 自动同步

`scripts/sync_omp_hook.py` 是 macOS 开发工具，不属于 AstrBot 运行插件，不进入插件 ZIP。它用 Python 标准库把一份 `.ts` 源文件单向同步为普通文件部署副本，不执行 Hook、不修改 Git index、不提交或推送、不重启 OMP。

### 前提与首次安装

- 使用主硬盘上的 Python 3.10 或更新版本，不通过外置仓库的 `.venv` 启动安装命令；解释器的真实路径位于 `/Volumes/` 时安装会拒绝。
- 源文件必须存在、非空且不超过 4 MiB，目标父目录必须存在；源、目标及其路径组件不能是符号链接，两端不能是同一个文件。
- 首次安装时，目标应不存在或与源内容相同。已有不同内容返回 `initial_target_conflict`，不覆盖；先人工核对并将旧文件备份到自动发现目录之外。
- 维护源为 `integrations/omp/webhook-notifier.ts`；源文件存在不等于已经部署或启用同步，首次接管需完成下面的安装、同步和启动步骤。
- 从旧 `onebot.ts` 切换到新文件名时，应另行备份并移走旧加载入口，避免两份 Hook 同时自动加载。同步器不会替调用方处理旧入口。

在插件仓库根目录执行：

```bash
python3 scripts/sync_omp_hook.py install \
  --source "$PWD/integrations/omp/webhook-notifier.ts" \
  --target "$HOME/.omp/agent/hooks/post/webhook-notifier.ts"
python3 scripts/sync_omp_hook.py sync
python3 scripts/sync_omp_hook.py start
python3 scripts/sync_omp_hook.py status
```

`install` 只安装，不启动或立即复制 Hook。首次 `sync` 确认部署结果后再 `start`；后台启动也会自行完成首次稳定观察与同步。

### 管理与数据归属

默认实例为 `default`。源码入口或主硬盘运行副本均可管理同一实例：

```bash
SYNC="$HOME/Library/Application Support/WebhookNotifier/omp-hook-sync/default/sync_omp_hook.py"
python3 "$SYNC" status
python3 "$SYNC" stop
python3 "$SYNC" start
python3 "$SYNC" sync
python3 "$SYNC" sync --overwrite-target
python3 "$SYNC" uninstall
```

| 动作 | 行为 |
| --- | --- |
| `install` | 安装／更新主硬盘运行副本和 LaunchAgent；更新前先 `stop`，同一实例不能改绑源或目标 |
| `start` | 启用并加载用户级 LaunchAgent；登录会话中运行，意外退出由 launchd 重启 |
| `stop` | 禁用并卸载任务，保留部署文件；再次 `start` 才恢复后台同步 |
| `status` | 输出是否安装、是否已加载、最后同步时间／摘要、冲突标记和当前两端内容是否一致；成功查询退出 0，即使同步处于冲突状态 |
| `sync` | 立即尝试同步；需要稳定观察时等待 2 秒，未完成或存在冲突时退出 2 |
| `sync --overwrite-target` | 显式处理冲突；现存目标先备份，再以稳定源覆盖，目标已删除时不伪造备份 |
| `uninstall` | 停止任务，删除 plist 和运行脚本；保留目标、配置、状态和备份，不清理用户文件 |

运行目录为 `~/Library/Application Support/WebhookNotifier/omp-hook-sync/<实例>/`，包含 `config.json`、`state.json`、运行脚本和锁文件；冲突备份位于其中的 `backups/`，使用 `.bak` 后缀。正文不进入状态文件，状态只记录路径、摘要、时间和固定状态码；备份本身包含目标原文，应按本机私密资料保管，工具不自动清理备份。

默认 LaunchAgent 为 `~/Library/LaunchAgents/io.github.asterleedsguild0.webhook-notifier.omp-hook-sync.plist`。`--instance <小写字母数字连字符名称>` 放在子命令之前，可创建隔离验收实例；非默认实例在 LaunchAgent label 后追加实例名。不要让多个实例管理同一目标。

更新同步器本身需停止后，从插件仓库重新运行相同绑定的 `install`，再 `start`。它不会自行更新运行脚本。卸载后配置与冲突标记仍保留，相同绑定重新安装不会清除冲突；卸载后如需重新安装，应使用仓库脚本入口。

### 同步与故障语义

- 每 2 秒读取一次小文件；相邻两次内容一致才复制，因此通常在稳定保存后约 2–4 秒同步。连续保存、原子保存或分支切换都会重新核对内容，不依赖旧 inode。
- 写入目标目录的 `.tmp` 文件后原子替换，不留下可被 OMP 自动加载的临时 `.ts` 文件；部署文件权限为 `0600`，内容不变时不重写目标。
- 源缺失、外置盘断开或源变为空文件时，保留最后部署副本；恢复有效源后自动继续。该稳定观察不是 TypeScript 编译或功能验证，保存的开发版本仍可能不可用。
- 目标内容与上次成功部署摘要不同或目标被删除时，锁存 `target_conflict`，即使进程重启或后来内容偶然一致也不会自动恢复。检查后显式使用 `sync --overwrite-target`；源不可用时不会强行覆盖。
- 源／目标为符号链接、非普通文件、超限或发生 I/O 错误时，不继续写目标，状态显示固定错误码；状态损坏时不猜测基线。
- 单实例锁阻止重复后台进程；同步操作锁协调后台检查和手动同步。工具在替换前再次检查两端，但不与外部编辑器共享锁，不能保证消除检查与替换间极短的竞争窗口。不要同时编辑部署副本和开发源。
- 替换目标后、写状态前若进程崩溃，下次会保守识别为目标冲突，需要人工核对恢复，不静默重新建立基线。
- `loaded=true` 表示 launchd 已加载任务，不是 OMP 已加载新 Hook 的证明；后台故障时还应检查该 label 的 `launchctl print`。文件同步不保证当前 OMP Session 热更新，应新开 OMP 进程验收。

### 验收范围

2026-10-22 已完成 26 项同步器回归、全仓 1226 项 Python 测试及 Ruff 检查。真实 macOS 用户级 LaunchAgent 使用外置盘临时源和主硬盘临时目标，验证了自动同步、原子保存、重复进程拒绝、崩溃重启、冲突跨重启保留、备份覆盖、源消失恢复、停止、单次同步和卸载保留数据。临时服务、plist 与实例文件已清理，原 `onebot.ts` 内容未改变。

2026-10-22 已完成本机真实入口切换：旧 `onebot.ts` 移至默认实例的 `backups/onebot-before-cutover-20261022.ts.bak`，SHA-256 与切换前一致；新入口为 `~/.omp/agent/hooks/post/webhook-notifier.ts`，是权限 `0600` 的普通文件，与外置盘维护源内容一致。默认 LaunchAgent 已启动，检查时进程运行且状态为 `in_sync`、无冲突；OMP 配置仓库的忽略规则已放行新文件。

该验收只覆盖部署文件、备份及同步服务，不证明当前 OMP Session 已重新加载 Hook。OMP 原生加载、线上投递、登录重启恢复和物理磁盘拔插未执行；此前源消失恢复使用临时路径操作模拟。

---

## 相关链接

- [端到端部署教程](end-to-end-setup.md)
- [项目 README](../README.md)
- [安全与运维指南](security-and-operations.md)
- [仓库维护的 OMP Hook](../integrations/omp/webhook-notifier.ts)
- [OMP Hooks 文档](https://omp.sh/docs/hooks)
- [OMP 官方 Extension Loading 文档](https://github.com/can1357/oh-my-pi/blob/main/docs/extension-loading.md)
- [OMP 官方 Hooks 文档](https://github.com/can1357/oh-my-pi/blob/main/docs/hooks.md)
