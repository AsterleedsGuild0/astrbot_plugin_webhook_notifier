# OMP V2 分层验收记录

## 基线与隔离

- 实施起点：插件 `1e68c1378671f4209e582474ffc4440db2c14a7d`；OMP 配置仓库 `5ba940728da73ce962134422a0403f3a0dc32c7f`。开始时均干净，分别领先远端 2／1 个提交；未替换本地基线、未提交或推送。
- 实际二进制：隔离 HOME 和环境执行 `omp --version`，返回 `omp/18.12.6`；Bun 测试输出版本 `1.3.14`，Python `3.13`。
- 默认同步器只读核对：`loaded=false`，launchctl 为 disabled；`in_sync` 仅为历史同步记录。真实部署入口仍为普通文件、权限 `0600`，SHA-256 `aaf06e753f9428eea0794dea2db586d4b38f2445480c8a49fbb37373461d9c99`。开发未改该文件、未恢复同步或旧入口。
- 本地验证数据归既有 `tmp/`；HTTP smoke 的临时 HOME／Registry／driver 自动清除。未读取线上凭据，未部署或修改 Issue。

## #29：V2 通知闭环

### 变更组

- `integrations/omp/webhook-notifier.ts`：独立 V2 事件、最终运行状态、交互配对去重、匿名白名单、快速回调、有界队列／并发／重试、HTTP 与业务结果分类。
- `core/omp.py`、`core/display.py`、`core/sender.py`：严格 V2、V1 兼容纠错、默认状态展示、OMP 投递异常隐私。沿用现有 Server、策略、Sender 和 renderer，不新增服务端状态机。
- Bun／Python 回归、`scripts/smoke_omp_hook.py` 及对应契约、接入、运维文档。旧版仅固定成功文案／raw 映射的 characterization 断言移除，保留实际兼容与行为检查。
- 本地变更组快照保存在 `tmp/omp-upgrade/`；未暂存、提交或发布。快照是本地审阅材料，不是安装包或部署证明。

### 已执行验证

| 层级 | 场景与预期 | 实际结果 | 证据入口 |
| --- | --- | --- | --- |
| 本地确定性 Hook | 正常／失败／中止／未知、续跑与后台等待、重试恢复／失败、最终事件交错、会话切换、元数据变化 | OMP Bun 41 项通过；无提前完成或串会话，缺开始不伪造耗时 | `bun test integrations/omp/webhook-notifier.test.ts` |
| 本地交互与资源 | 审批／计划／ask 快速解决、取消、无 UI、重复／反序、发送前撤销、重试撤销、并发 2／队列 64、getter／网络同步故障 | 计入上述 41 项，全部通过 | Hook 行为测试 |
| 跨客户端回归 | OpenCode 行为不变 | 两客户端运行曾为 316 项通过（OMP 37＋OpenCode 279）；新增 4 项资源边界后 OMP 单独 41 项通过 | Bun 命令输出 |
| 本地 Python | V1/V2 区分、严格 shape／版本／时间／上限、规范状态、短成功过滤、现有 Provider／路由／展示 | 全仓 1282 项通过；Ruff 通过 | `uv run pytest -q`、`uv run ruff check .` |
| 真实回环 HTTP | Hook → 实际 HTTP → Endpoint 鉴权 → OMP Adapter → 策略 → 文本 → Sender → 内存消息接收器 | 完成／失败／中止均报告 delivered；失败／中止文本不显示完成 | `uv run python scripts/smoke_omp_hook.py` |
| HTTP 故障与幂等 | 401/403 永久错误；429/503 有界重试；超时不能推断未发送；重放不能证明送达 | 401/403 各 1 次，429/503 各 3 次；丢响应／超时后第 2 次为 replay，目标仅发送 1 次；各次 ID 相同 | smoke 的 13 个场景 JSON 回执 |
| HTTP 业务分类 | 时长策略跳过、部分失败／部分策略投递、200 非协议正文 | 分别为 policy_skipped、partial_failure、partial_delivery、unknown；不换 ID 补发 | 同上；partial_delivery 使用受控 HTTP 业务响应 |
| 隐私哨兵 | 出站、客户端日志、接收端日志、标准化、默认文本／HTML 均无正文／完整路径／原始 ID／错误 | 通过；真实 HTTP 最初暴露 Sender 的旧 traceback 泄漏，修正 OMP 分支后重跑通过 | `tests/test_omp_v2.py`、HTTP smoke |

### 未覆盖与限制

- 原生 OMP：本节为本地确定性层；原生 producer 的生命周期、审批／计划／ask 结果见下文[原生 OMP 验收](#原生-omp-验收)。Bun 注入公开事件只是确定性回归，不冒充原生覆盖。
- 线上 AstrBot：未部署、未使用专用测试 Endpoint／线上凭据、未核对 QQ 实际消息。HTTP smoke 的消息边界是内存接收器。
- #29 的默认 HTML 字段检查不是视觉验收；真实渲染表面由 #31 单独记录。
- V1 `stop` 缺最终续跑证据，保留 unknown；只有 V2 规范 completed 进入最短时长过滤。重试取消／耗尽没有公开结构化判别字段，不读取原始错误猜原因。
- #29 已达到后续本地开发依赖的接口和行为基线；上述原生与线上缺口不勾选为完成，不关闭 #29 或总任务。

## #30：多代理与交互观察时间线

### 变更组

- 沿用 #29 的 Hook Run、交互 Map、周期和发送队列；增加公开 parent＋owner snapshot 关联、单调时钟观察段、根聚合和有界保留。普通 child 完成不单独发送，异常和待处理仍走 V2。
- `core/omp.py` 严格验证可选组件，复用既有 `subagent_timeline`／`user_wait_timeline` 标准化字段；没有第二套 wire 协议或服务端运行状态机。没有复制 OpenCode 采集器或 renderer。
- `tests/test_omp_timeline.py`、Bun 行为回归和同一 HTTP smoke 扩展；现有公共契约与接入说明同步。独立增量补丁、源码快照及 HTTP 回执保存在 `tmp/omp-upgrade/30-*`。

### 已执行验证

| 层级 | 场景与预期 | 实际结果 | 证据入口 |
| --- | --- | --- | --- |
| 本地确定性采集 | 批量／并发／嵌套、eval 来源实例、worker 身份复用、多个同名 Main 根、后到关联证据、歧义保持 | OMP Bun 57 项通过；child 无 session_stop 仍收束，普通完成仅根通知，旧 cycle 不合并 | `bun test integrations/omp/webhook-notifier.test.ts` |
| 边界与资源 | 开始／结束缺失、恢复无历史、child shutdown、重复交互、后台续跑、深度 10、扇出 80、字节上限、24 小时过期、墙钟跳变 | 不补造边界；深度仅显示 8；截断可见；过期不因迟到结束复活；墙钟跳变仍保持单调耗时 | 同上 |
| 规范数据／统计 | 拒绝非法结构、未知字段、布尔／负值／NaN 时长、父图、重复、越界、元数据混合、条数与组件字节超限 | OMP V2＋timeline Python 122 项通过；并发／嵌套 18s 段时长之和仅计 8s 覆盖并集 | `uv run pytest -q tests/test_omp_v2.py tests/test_omp_timeline.py` |
| 全仓回归 | 既有 OpenCode、Markdown、V1、路由、渲染不变 | Python 全仓 1340 项通过；Ruff 通过 | `uv run pytest -q`、`uv run ruff check .` |
| 真实回环 HTTP | 收集器 → Server → OMP Adapter；并发＋嵌套＋续跑＋worker 复用＋ask／后台等待 | 17 场景通过；timeline 收到 5 段／4 cycle，覆盖并集 14s；待处理→收束各自独立 ID，故障重试保持原 ID | `scripts/smoke_omp_hook.py`；`tmp/omp-upgrade/30-http.json` |
| 隐私 | 参数、输出、完整路径、原始身份与错误哨兵不进入 wire／日志／默认消息 | 上述 Bun、Python 和 HTTP 检查通过；快照 label／command／调度时间未用于外发 | 相同行为与 smoke 断言 |

### 未覆盖与限制

- 上述“批量／eval／worker”来自确定性公开事件序列；实际 task、eval `agent()` 与 workpool 调度的原生观察见下文[原生 OMP 验收](#原生-omp-验收)。
- 仅关联已加载且证据唯一的实例。快照为 owner scoped 有界可见记录，不能恢复全历史；未加载、跨模块副本、跨进程、旧事件缺 cycle 身份等情况不宣称精确归属或完整计时。
- #30 的规范字段和区间并集已验收；卡片入口、分类标签、真实渲染和附图降级由 #31 验证。线上目标消息仍未授权、未执行。
- #30 已达到 #31 本地展示集成的依赖基线；原生与线上验收未勾选，Issue 与总任务保持未完成。

## #31：共用卡片与分层集成

### 变更组

- `core/renderer.py`：解除 OpenCode 专属入口限制，复用同一主卡、时间线模板、布局和统计；OMP 使用观察标签、周期／段、实际 thinking 与四类独立轨道，缺口不显示精确零。文本与图片保持关键事实一致。
- Hook 增加公开元数据采样边界，不消费 `before_provider_request.payload`；周期中切换后恢复原值仍标记混合。`tests/test_omp_rendering.py` 覆盖并集、分类、缺失、混合元数据和主卡／附图降级；修正旧 Server 测试中混用 OMP Provider 与 OpenCode 事件的输入，没有放宽生产校验。
- 隐私边界补充：客户端与 `core/omp.py` 的模型路径判定覆盖 `provider/` 前缀后的绝对路径、用户目录和 Windows 路径；正常 `provider/org/model` 保留，不把路径作为模型名称展示。
- 公共契约、接入说明、发布门禁／回滚与更新日志同步；独立增量补丁和源码快照保存在 `tmp/omp-upgrade/31-*`，继续保留 #29／#30 的独立快照。未提交或暂存。

### 已执行验证

| 层级 | 场景与预期 | 实际结果 | 证据入口 |
| --- | --- | --- | --- |
| 本地 Hook | 实际 thinking 采样、元数据切换后恢复、全部 #29／#30 行为 | 两客户端 Bun 341 项通过（OMP 62、OpenCode 279），Bun 1.3.14 | `bun test integrations/omp/webhook-notifier.test.ts integrations/opencode/webhook-notifier.test.ts` |
| Python／跨 Provider | OMP 五状态、并集、四类轨道、缺口／混合元数据、附图／主卡失败；既有 OpenCode、Markdown、V1 与路由 | Python 3.13 全仓 1355 项通过，Ruff 通过 | `uv run --frozen pytest -q`、`uv run --frozen ruff check .` |
| 实际浏览器表面 | 完成／失败／中止／待处理／未知、并发／嵌套、长名称、64 段＋64 交互、深度 8、partial／截断，以及 OpenCode、Markdown、V1 | Chrome 150.0.7871.24：15 个主卡／附图表面无水平溢出、名称越界或条带越界；已查看实际截图，非只检查模板字符串 | `tmp/omp-upgrade/31-browser.json`、`render/*.png` |
| 真实 HTTP＋图片 | 维护源 Hook → Server → 标准化 → 主卡／附图 HTML → 离线 Chromium PNG → 校验／trim → Sender | Chrome 155.0.8059.85：HTTP 200、业务 delivered，内存消息边界收到同一消息的两张 Image；含 5 段／4 周期，覆盖并集 14s | `tmp/omp-upgrade/31-image-http.json`、`render/http-images-*.png` |
| 真实 HTTP 降级 | 附图渲染失败不得丢主卡；主图失败回退文本 | 两场景均 delivered；分别为一张主 Image、一个 Plain，文本保留 14s 并集和观察语义，错误哨兵不出日志 | 同一图片 HTTP 回执 |
| 旧协议拒绝 | 从未推送的本地基线加载旧 OMP Adapter，在真实 Server HTTP 链路接收新 Hook V2 事件 | HTTP 400，客户端 permanent_error，不重试、不发送目标消息；未把新事件误认成 V1 成功 | 回执 `old_adapter_v2`，旧 Adapter 来源 commit `1e68c1378671f4209e582474ffc4440db2c14a7d` |
| 隐私 | 出站、客户端／接收端日志、文本、HTML／浏览器可见文字不得包含正文／原始身份／路径／错误哨兵 | 通过；截图由检查过的实际 DOM 生成，无 raw 补正文；不把 PNG 压缩字节搜索当作图片隐私验收 | Bun／Python、浏览器及真实 HTTP 断言 |
| 模型路径回归 | provider 前缀不得隐藏原始文件路径 | 修复前客户端 4／服务端 8 个用例失败，修复后全部通过；另用绝对模型路径哨兵执行 17 个真实 HTTP 场景，出站／日志／消息不含路径 | 新增 Bun／Python 回归、`tmp/omp-upgrade/31-privacy-http.json` |
| 线上 AstrBot 投递 | 2026-10-23 经 `scripts/deploy_dev.py` 用 Dashboard Open API（仅 `plugin` scope）在线上 AstrBot v4.27.2 卸载（保留配置与数据）并上传 `v2.0.0-test.20261023.1121.omp-concise`，向现用私聊 OMP Endpoint 发送合成 V2：简单完成、多代理时间线、失败 | 线上加载目标版本；3 条均 HTTP 200、`delivered=true`、`render_mode=html_image`。QQ 私聊实际收到 3 张主卡和 1 张时间线附图：简单运行无时间线区域，多代理主卡只有摘要与交互汇总，附图分轨正确，失败卡显示“运行失败”；接收端视觉经人工确认 | 部署脚本输出；私聊截图 |

测试包按 [发布门禁](release.md#omp-v2-测试包与切换门禁未发布)构建。具体测试版本、单根目录、版本三源、运行文件与文档哈希、提取入口 HTTP 回归结果以 `31-package.json`／`31-package-http.json` 为准；这些回执与 ZIP 都是本地产物，不是发布或安装证明。

### 未覆盖与验收门禁

- 原生 OMP：实际版本 18.12.6 的真实 producer 场景已在隔离环境执行，结果见下文[原生 OMP 验收](#原生-omp-验收)。
- 线上 AstrBot：已验证插件安装、真实 T2I 渲染和私聊目标消息；原生 OMP 产生的线上私聊通知见下文。群目标及 OpenCode／Markdown 的线上回归未单独记录。
- 旧端拒绝证据使用基线 Adapter＋当前 Server 传输链路，不声称重新安装并启动过整个旧版本 AstrBot。
- 卡片布局与显示结论仅限已列出的有界输入和浏览器版本；时间线表示可见观察，不保证完整调度历史、精确人工等待或业务目标成功。
- #29 → #30 → #31 的本地依赖工作已经推进，线上合成投递已通过；原生场景结果与剩余缺口见下文。

## 原生 OMP 验收

### 环境与隔离

- 日期 2026-10-24；OMP `omp/18.12.6`；线上 AstrBot v4.27.2 已加载测试包 `v2.0.0-test.20261024.1448.caption-inline`。客户端源为本仓库 `integrations/omp/webhook-notifier.ts`，验收过程中先后为 `9b9b1c4`、`03b1ac9`、`9ea46cf`、`b210195`（各场景注明）。
- 隔离：子进程环境从空白构造，`HOME` 与 `PI_CODING_AGENT_DIR` 指向仓库 `tmp/omp-native/home/`，`--no-extensions --no-skills --no-rules`，`--hook` 显式加载仓库 Hook，cwd 为 `tmp/omp-native/work/<场景>`。未继承调用方 shell 中的生产 `OMP_SESSION_WEBHOOK_*`。真实 `~/.omp` 配置与 `hooks/post/webhook-notifier.ts` 未改动，后者 SHA-256 仍与 `bdcae29` 中的 Hook 一致；Hook 同步器保持停止。
- 模型：隔离 `models.yml` 只含本机 CPA provider 结构，`apiKey` 写环境变量名，由启动进程从用户 shell 环境只读注入，凭据不落盘。模型 `cpa/deepseek-flash`，thinking `low`。
- 测试目标：`verify-receiver` 中的测试 Endpoint（官方 Bot「626」私聊接收账号），不是生产 Endpoint。Hook 发往本机 `127.0.0.1` 记录代理，代理原样转发并记录请求体与响应，不记录 Token。模型请求经本机故障代理转发到 CPA，仅重试场景注入 HTTP 错误。
- 观察：另以 `-e` 显式加载一个仅供验收的事件记录器，记录公开事件类型与结构字段（stopReason、willContinue、awaitingAsyncWork、toolName、审批／计划结果、子代理身份），不记录正文、参数或输出，不返回控制值。驱动方式：RPC（`--mode rpc-ui`，Hook 可见 `hasUI=true`）；计划评审只在交互 TUI 中可达，用伪终端驱动。
- 读回：`.agents/scripts/tencent3year-napcat-receiver-probe messages <since> 原生验收`。会话名统一以“原生验收 <场景>”开头，图片前文字即 `【状态】会话名`。每次读回最多下载 8 张图片，未下载的图片只核对存在与顺序。
- 原始证据保存在 `tmp/omp-native/logs/`（`events.jsonl`、`webhook.jsonl`、`model.jsonl`、`runs.jsonl`、各场景 RPC／TTY 日志、`probe-*.json`），本地产物，不随 ZIP 分发。

### 场景结果

“通知”列为 Hook 实际发出的 V2 请求与服务端响应；“接收端”为读回的同条消息文字与图片。服务端默认最短完成时长 15 秒，短于该值的完成通知按设计跳过。

| 场景 | OMP 实际公开事件 | 通知 | 接收端 |
| --- | --- | --- | --- |
| 正常结束 | `agent_start` → bash → `session_stop(stop)` → `agent_end(stop)` | `run_finished` completed，17.4s，无缺口；200 `ok` delivered | 【已完成】原生验收 S1，主卡 1056×711 |
| 失败（注入 400） | `session_stop(error)` → `agent_end(error)` | failed，33ms；delivered（失败不受时长过滤） | 【失败】原生验收 S2-failed |
| 中止（RPC `abort`） | bash 结果 isError → `agent_end(aborted)`，无 `session_stop` | aborted，19.6s；delivered。`b210195` 复测：暂缓 150ms 后照常送达，耗时 19.9s 仍按 `agent_end` 计 | 【已中止】原生验收 S2-aborted |
| 审批批准 | `tool_approval_requested` → 12s 后 `tool_approval_resolved(approved)` | `action_required` approval（150ms 后）；完成 completed 21.8s，等待区间 `approval:resolved:complete`；均 delivered | 【待处理】… · 等待工具审批；【已完成】主卡＋时间线附图 1872×901 |
| 审批拒绝 | `tool_approval_resolved(approved=false)`，模型随后正常结束 | `action_required` delivered；完成 completed 14.1s，`approval:cancelled` → 200 `skipped`（`completion_below_duration_threshold`） | 仅待处理卡；完成卡按策略未发 |
| ask 回答 | `tool_call(ask)` → 20s 后 `tool_result(ask)` | `action_required` ask，携带题干、标题与选项；完成 completed 21.6s，`ask:resolved` | 待处理卡显示 Color／Which color do you prefer?／1 Red 2 Blue；完成卡＋附图 |
| ask 取消 | 取消后 `tool_result(ask, isError)`，OMP 随即 `agent_end(aborted)` | `action_required`；最终 aborted，`ask:unknown`（无结构化取消字段，按契约不读错误猜原因）；`b210195` 复测结果相同 | 【待处理】；【已中止】＋附图 |
| 计划评审批准（TUI） | `/plan` 后 `agent_end(aborted)` → `plan_review_requested` → 15s 后 `plan_review_resolved(execute)` → `session_switch(new)`，新会话执行并 `agent_end(stop)` | `b210195`：只有 1 条 `action_required` plan_review，周期无缺口（5.7s）；旧周期静默收束；新会话 completed 3.5s 按时长策略跳过 | 仅【待处理】… · 等待计划审阅，卡片显示计划标题 |
| 计划评审取消（TUI） | `agent_end(aborted)` → `plan_review_requested` → Esc 后 `plan_review_resolved(choice 缺省)`，之后无运行事件 | `b210195`：`action_required` 后 1 条 aborted 19.2s，`plan_review:cancelled:complete` | 【待处理】；【已中止】＋附图，交互区间“计划审阅 1” |
| 计划评审保存（TUI，`b210195`） | `agent_end(aborted)` → `plan_review_requested` → 选“Save and quit”后 `plan_review_resolved(choice=save)`；OMP 随即弹出保存路径对话框，确认路径后写入计划文件并 `session_switch(new)`，之后无运行事件 | 只有 1 条 `action_required` plan_review（5.6s）；旧周期在 `session_switch` 时按交接静默收束，退出时没有补发【未知】 | 仅【待处理】… · 等待计划审阅 |
| 计划评审细化（TUI，`b210195`） | 选“Refine plan”后 `plan_review_resolved(choice=refine)`；输入细化意见后同一会话 `agent_start`，再次 `agent_end(aborted)` → `plan_review_requested`；第二次评审按 Esc 取消 | 2 条 `action_required`（同一周期，3.8s／31.4s），随后 1 条 aborted 46.8s，`cycle.reasons` 为空 | 两张【待处理】；【已中止】 |
| 重试恢复 | 注入 7 次 503：`auto_retry_start(1/10)` → `agent_end(willContinue, error)` → `agent_start` → `auto_retry_end(success)` → 正常结束 | 仅 1 条 completed，40.6s | 【已完成】原生验收 S4-retry-recovered |
| 重试耗尽 | 10 次 `auto_retry_start`，每次 `agent_end(willContinue, error)`；`auto_retry_end(success=false)` → `agent_end(error)` | 仅 1 条 failed，217s；中间续跑不发送 | 【失败】原生验收 S4-retry-exhausted |
| 后台续跑 | 异步 bash 后 `agent_end(willContinue, awaitingAsyncWork)`；任务完成后 `agent_start` → `agent_end(stop)` | 1 条 completed 21.9s，`background_wait:resolved:complete` | 【已完成】＋附图 |
| task 多代理 | 父会话 `task` 派生 2 个 `sub` depth 1，子代理 `agent_end(stop)`，无 `session_stop` | 子代理不单独通知；根 completed，时间线 2 段 completed | 【已完成】原生验收 S5-task，主卡 1056×1175 |
| eval `agent()` | `before_subagent_spawn(invocationKind=eval)` ×2；子代理以 `yield` 提交后 `agent_end(toolUse)` | `9b9b1c4`：子代理各发一条 unknown，根时间线为未知（缺陷）；`9ea46cf`：仅根 completed，2 段 completed | 修复前 2 条【未知】；修复后 1 条【已完成】 |
| workpool | 2 个 worker `yield` 成功后 OMP 中止后续回合，`agent_end(aborted)` | `03b1ac9`：每个 worker 单独 aborted（缺陷）；`9ea46cf`：仅根 completed，2 段 completed | 修复前 4 条【已中止】；修复后 1 条【已完成】，时间线显示 accept-pool-1／2 已完成 |

重试恢复首次只注入 1 次 503，被 provider 传输层重试吸收，未触发 `auto_retry_*`；每次自动重试内含 6 个传输层请求，因此改为注入 7 次。

### 原生验收发现与修正

- 默认 5 秒超时（`03b1ac9` 修正）：服务端同步渲染并投递，单图实测约 9～10 秒、主卡加附图约 19～21 秒。5 秒默认使已送达的请求超时重试，服务端返回 `idempotency_replay`；改 15 秒后附图场景仍触发同样回放。默认改为 30 秒后，后续场景均为单次 delivered。
- 子代理 `yield` 收束（`9ea46cf` 修正）：eval 子代理结尾为 `toolUse`、workpool worker 结尾为 `aborted`，原映射分别得到 unknown／aborted 并单独提醒。成功 `yield` 后的这两种结尾现计为完成；`yield` 失败仍按原状态。
- 退出中断：运行中关闭 RPC stdin 时只观察到 `session_shutdown`、没有 `agent_end`，Hook 按 `shutdown` 生成的 unknown 未在进程退出前发出；TUI 以 Ctrl-C 退出的同类场景则送达。与“退出只尽力移交”的契约一致，未改动。
- 计划评审（`b210195` 修正，按方案 A 取舍）：TUI 中 OMP 先以 `agent_end(aborted)` 收束本轮，再打开评审；批准后切到新会话执行，取消后不再有运行事件。修正前 `9ea46cf` 的实际通知为：评审前【已中止】、缺开始的【待处理】，以及批准后（`session_changed`）或退出时（`shutdown`）的【未知】。现在交互式根运行的中止暂缓 150ms，同会话随即打开计划评审则不发中止，评审沿用原周期；批准后旧周期静默收束，由执行会话通知；取消评审按中止报告。V2 线协议未改。

### 未覆盖

- 群目标、OpenCode／Markdown 的线上回归、插件市场安装未在本轮执行。
- 审批“为本会话批准”、eval 间接 ask、嵌套多层子代理、上下文压缩与会话分支未在原生环境触发。
- 计划评审“保存”“细化”已于 2026-10-24 实测（见上表），没有出现“一直不收束”：保存在确认路径后立即切到新会话，旧周期静默收束；细化在同一周期内继续，最终按第二次评审的结果收束。保存后不再发送收束通知，与“批准后交给执行会话”的静默交接一致，不作为缺陷。保存路径对话框中按 Esc 的分支未测。
- 接收端图片只人工抽查了 ask 待处理卡、ask 完成卡、计划中止卡（修正前）、计划待处理卡、计划取消卡与 workpool 完成卡；其余仅核对文字、同条与图片尺寸。
- 上述未覆盖项的补测由 #37 跟踪。

## #32：OMP 客户端改为 extension 包

### 变更组

- `integrations/omp/` 改为带 `package.json#omp.extensions` 的包，入口 `index.ts`。原单文件按职责拆为 `index.ts`（事件订阅与收束判定）、`transport.ts`（配置／传输）、`state.ts`（运行与交互状态、父子关联）、`timeline.ts`（时间线采集）、`sanitize.ts`（清洗与匿名化）、`wire.ts`（V2 出站类型）。函数体逐字搬移，handler 的同步返回与调用顺序不变；V2 线协议、事件订阅、状态判定、隐私白名单和发送策略未改。
- Bun 行为回归改名为 `index.test.ts`，只改了导入路径，断言未动。新增打包契约测试：从清单入口沿相对导入求闭包，要求闭包内文件全部进入插件 ZIP、且 ZIP 不含 `*.test.ts`。
- `scripts/package_plugin.py` 打包整个客户端目录（排除测试与隐藏文件）；`scripts/smoke_omp_hook.py` 改为 `--package <目录>`，按清单解析入口。
- `scripts/sync_omp_hook.py` 由单文件同步改为包目录同步：整包摘要、隐藏暂存目录写入后两次 rename 换入、冲突时整目录备份。源缺失不删目标、目标独立修改后锁存暂停等保护行为保留，回归改写为目录形态（30 项）。
- 原生验收工具从 `tmp/` 整理进 `scripts/omp_native/`，客户端加载方式由 `--hook <文件>` 改为 `--no-extensions -e <包目录>`；另支持 `OMP_NATIVE_LOAD=installed`，使用隔离 HOME 中自动发现或安装的客户端。

### 已执行验证

| 层级 | 场景与预期 | 实际结果 | 证据入口 |
| --- | --- | --- | --- |
| Bun 行为回归 | 断言不改，覆盖全部既有行为 | OMP 68 项、OpenCode 279 项，共 347 项通过 | `bun test integrations/omp integrations/opencode` |
| 类型检查 | 以 OMP 18.12.6 发布包的类型声明检查 6 个模块（strict、`noUnusedLocals`） | 客户端模块 0 错误 | `tmp/omp-types/`（本地，不入库） |
| 出站一致性 | 同一回环 HTTP smoke 分别驱动 v2.1.0 单文件与新包入口，归一化随机引用与时间戳后逐字节比较请求体 | 17 个场景的请求体完全一致；结果分类、请求次数与重试同 ID 均一致 | `tmp/compare-32/`（本地） |
| 回环 HTTP | 包入口与测试 ZIP 解压出的包目录 | 两者 17 场景均通过 | `scripts/smoke_omp_hook.py --package …` |
| Python 全仓 | 打包契约、同步器目录形态回归 | 1383 项通过，Ruff 通过 | `uv run --frozen pytest -q` |
| 原生 OMP（显式包） | `--no-extensions -e integrations/omp`，S1 正常结束、ask 回答 | S1 1 条 completed（55.8s）；ask 1 条 `action_required`＋1 条 completed，`ask:resolved:complete`；均 delivered | 【已完成】原生验收 S1；【待处理】／【已完成】原生验收 S3-ask-resolved |
| 新旧入口并存 | 隔离 HOME 同时放 v2.1.0 的 `hooks/post/webhook-notifier.ts` 与同步出的 `extensions/webhook-notifier/` | 同一次运行发出 2 条 completed，接收端收到 2 条【已完成】原生验收 S6-old-and-new | 证实迁移文档中“重复发送”的风险 |
| 迁移后单入口 | 删除旧 Hook，只保留包目录 | 1 条 completed，接收端 1 条 S6-package-only | 同上 |
| 外置盘未挂载 | 用真实 `Synchronizer` 从模拟外置卷同步到隔离 HOME 后，重命名卷目录 | 同步状态 `source_unavailable`，目标保留；OMP 照常加载并发出 1 条 completed（S6-source-unmounted） | `tmp/omp-native/sync_accept.py`（本地） |
| 目标被独立修改 | 修改部署副本后恢复源卷 | `target_conflict` 锁存，部署副本保留本地修改，未被覆盖 | 同上 |

接收端读回：`probe-32.json`，7 条消息与上表一一对应，每条一张 PNG。

### 未覆盖与限制

- 本机真实入口仍是 v2.1.0 单文件 Hook，同步器保持暂停；切换到包目录与恢复同步在发版后进行。
- 外置盘未挂载以重命名目录模拟，未做物理拔盘；登录重启后 LaunchAgent 的恢复未测。
- 符号链接方案未采用：OMP 18.12.6 的扩展目录发现会跳过悬空链接（`discovery/helpers.ts`），盘未挂载时客户端会静默失效，属源码依据，未做原生实测。

## 原生验收工具

`scripts/omp_native/` 提供隔离环境下的原生 OMP 验收，日志、隔离 HOME 与工作目录在被 Git 忽略的 `tmp/omp-native/`。

```bash
export OMP_NATIVE_VERIFY_FILE=<测试 Endpoint 的 KEY=VALUE 文件>   # WEBHOOK_NOTIFIER_URL／WEBHOOK_NOTIFIER_TOKEN
uv run --frozen python scripts/omp_native/scenarios.py setup                 # 生成隔离 HOME
uv run --frozen python scripts/omp_native/scenarios.py proxies               # 前台运行本机代理
uv run --frozen python scripts/omp_native/scenarios.py S1 S3-ask-resolved    # RPC 场景；“场景:别名”可改会话名
uv run --frozen python scripts/omp_native/scenarios.py plan S3-plan save     # 计划评审：resolve|cancel|save|refine
```

- 子进程环境从空白构造，不继承调用方 shell 的生产 `OMP_SESSION_WEBHOOK_*`；Webhook 请求发往本机代理，再原样转发到测试 Endpoint，日志记录请求体与响应，不记录 Token。
- 模型 provider 只把真实 `models.yml` 中一个 provider 的结构写入隔离 HOME，`apiKey` 写成环境变量名，值启动时从登录 shell 只读注入，不落盘。`OMP_NATIVE_PROVIDER`／`OMP_NATIVE_MODEL` 可改 provider 与模型。
- `OMP_NATIVE_LOAD=package`（默认）只显式加载仓库客户端包；`installed` 不加 `--no-extensions`，使用隔离 HOME 中的扩展目录或已安装插件。
- `event-recorder.ts` 只记录公开事件类型与结构字段，不记录正文、参数或输出；每个场景在 `logs/runs.jsonl` 记切片索引。
