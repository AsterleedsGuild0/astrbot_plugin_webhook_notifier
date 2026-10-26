# 更新日志

## v2.4.0 - 2026-10-26

本版本为 OMP 通知的“本次运行用量”补充估算费用与响应速度（#39）。调查与对照结果见 [#39](https://github.com/AsterleedsGuild0/astrbot_plugin_webhook_notifier/issues/39)。

### 升级须知

- **先升级服务端，再升级客户端。** v2.4.0 客户端在 `signals.usage` 中新增 `costUsd`、`ttftMs`、`outputTps`，v2.3.0 及更早的服务端按字段白名单以 HTTP 400 拒绝这类请求（客户端视为永久错误、不重试，通知丢失）。新服务端继续接收不带这些字段的旧客户端，卡片与 v2.3.0 相同。
- 客户端通过 OMP marketplace 升级到 2.4.0 后需重启 OMP。

### 变更

- #39 OMP 本次运行用量另起两行显示“费用约 $…（按官方价格估算，含子代理）”与“首字 … s · 输出 … tok/s（客户端测得）”，纯文本同步。费用取 OMP 模型目录的估算值，合计主会话与已关联子代理，关联不完整、目录无定价或取不到时不显示；速度取本会话各请求首字延迟中位数与首字后输出速度，生成时长不足 1 秒时不显示输出速度。不维护价格表，也不显示 0。
- 原生验收工具（`scripts/omp_native/`）支持 `openai-codex` provider 的 `/backend-api` 路径，模型代理透传 WebSocket（此前每个会话首个请求要等两次约 10 秒的升级超时），注入故障时关闭已建立的隧道；事件记录增加每条助手消息的用量、估算费用与计时。

## v2.3.0 - 2026-10-26

本版本为 OMP Endpoint 增加深色通知卡片（#36）。创建与切换命令见[命令参考 · 卡片主题](https://github.com/AsterleedsGuild0/astrbot_plugin_webhook_notifier/blob/v2.3.0/docs/command-reference.md#卡片主题)。

### 升级须知

- **默认仍为浅色，已有 Endpoint 无需迁移。** Registry 记录新增可选字段 `theme`，旧记录缺少该字段时按浅色处理；浅色卡片与 v2.2.0 逐像素一致。Webhook 协议不变；OMP 客户端代码与 v2.2.0 相同，marketplace 中的 2.3.0 只是随插件版本同步，升级与否不影响通知。
- **回退到旧版本会丢失深色设置。** 旧服务端忽略 `theme`，下一次写回 Registry 时该字段被丢弃，卡片恢复浅色。

### 变更

- #36 OMP Endpoint 可选深色卡片：`token new … --provider omp --theme dark` 创建时指定，`token theme <名称> <light|dark>` 由 owner 在私聊切换；不写 `--theme` 时为浅色。只开放给 OMP Endpoint，OpenCode、Markdown Endpoint 使用主题参数、非 owner 切换、非法取值均拒绝且不修改 Registry。没有全局主题配置，不提供跟随系统的 `auto`。
- 深色采用“中性深灰”方案：外层 `#18191c`、卡片 `#171717`，表头与问题块略亮；待处理卡和凭据告警保留品牌黄状态条，推荐选项编号改为黄底深字；主文字约 `#e6e6e8`，次要文字满足 WCAG AA 对比度。主卡与时间线附图同一主题，页面外层背景随主题取色，深色卡片外不留浅色边。纯文本模式与文本降级不受影响。
- 内置主卡与时间线附图的颜色全部改为 CSS 变量，按根元素 `data-theme` 切换。模板上下文新增 `event.theme`，自定义模板可读取以自行支持深色；不读取该变量的模板输出不变。
- `token list` 对 OMP Endpoint 显示卡片主题，状态命令显示 active OMP Endpoint 按主题的数量，帮助卡增加 `token theme` 命令。
- `scripts/deploy_dev.py --verify` 增加 `--theme light|dark`，深色基准图位于 `tests/visual/baselines/dark/`；验收用例补充中止、未知和凭据告警，共 8 张图。`--update-baselines` 只补齐缺少的、覆盖不一致的基准图。

## v2.2.0 - 2026-10-25

本版本把 OMP 客户端改为多模块 extension 包并通过本仓库的 OMP marketplace 分发（#32、#33），通知补充错误类型、所属主会话、会话时长与运行健康信号（#34、#35）。原生 OMP 18.12.6 的验收结果见 [OMP V2 验收记录](https://github.com/AsterleedsGuild0/astrbot_plugin_webhook_notifier/blob/v2.2.0/docs/omp-v2-acceptance.md)。

### 升级须知

- **先升级服务端，再升级客户端。** v2.2.0 客户端会发送错误类型、资源信号等新增可选字段和凭据告警事件，v2.1.0 及更早的服务端会以 HTTP 400 拒绝这些请求（客户端视为永久错误、不重试，通知丢失）。新服务端继续接收旧客户端。
- **OMP 客户端改为 extension 包，手动复制过单文件 Hook 的用户需要迁移。** 客户端由 `integrations/omp/` 整个目录组成（`package.json`、入口 `index.ts` 与同目录模块），不再是单个 `webhook-notifier.ts`。迁移时先退出 OMP，删除 `~/.omp/agent/hooks/post/webhook-notifier.ts`（及项目级副本、旧 `onebot.ts`），再通过 OMP marketplace 安装（`/marketplace add AsterleedsGuild0/astrbot_plugin_webhook_notifier`、`/marketplace install omp-webhook-notifier@astrbot-webhook-notifier`），或把整个目录复制为 `~/.omp/agent/extensions/webhook-notifier/`，二者只选一种，然后重启 OMP。新旧入口同时存在会使每次运行发送两条通知。环境变量不变。步骤见[接入指南](https://github.com/AsterleedsGuild0/astrbot_plugin_webhook_notifier/blob/v2.2.0/docs/client-integration.md#从单文件-hook-迁移)。

### 变更

- #32 OMP 客户端拆为多模块 extension 包：`index.ts` 负责事件订阅与收束判定，配置／传输、运行与交互状态、时间线、清洗与 V2 类型各成模块；行为与出站内容不变（同一 smoke 下新旧请求体逐字节一致）。插件 ZIP 改为包含整个客户端包，不含测试。
- `scripts/smoke_omp_hook.py` 改用 `--package <目录>`，按 `package.json#omp.extensions` 解析入口。
- `scripts/sync_omp_hook.py` 改为同步整个客户端包目录到 OMP 扩展目录：整包摘要判稳、隐藏暂存目录写好后换入，冲突时整目录备份；源缺失或外置盘未挂载时保留部署副本，目标被独立修改后锁存暂停。
- 原生 OMP 验收工具整理进 `scripts/omp_native/`：隔离 HOME、本机代理、RPC／TUI 场景驱动与事件记录器；测试 Endpoint 从外部文件读取，不继承调用方 shell 的生产变量。
- #33 本仓库提供 OMP marketplace（`.omp-plugin/marketplace.json`）：`/marketplace add AsterleedsGuild0/astrbot_plugin_webhook_notifier` 后 `/marketplace install omp-webhook-notifier@astrbot-webhook-notifier` 安装客户端，`/marketplace update` 加 `/marketplace upgrade` 升级，安装或升级后需重启 OMP。版本取自 `integrations/omp/package.json`，发布门禁要求与插件版本一致。
- #34 OMP 通知补充错误类型、所属主会话与会话时长：失败卡按最终 assistant 的 HTTP 状态码（`errorStatus`）、`stopReason=length` 与自动重试结果显示“错误类型”，没有结构化依据时不显示，不读取错误原文；子代理异常／待处理卡在父子关联可证明时显示“所属主会话”；卡片显示“会话开始时间”与“会话已持续”（会话创建至今，恢复旧会话按原创建时间计算）。均为 V2 可选字段。新增客户端配置 `OMP_SESSION_WEBHOOK_ACTION_CONTENT`：默认 `summary`，待处理提醒内容与 v2.1.0 相同；设为 `strict` 时只发送交互类别。
- #35 OMP 收束卡补充运行健康与资源信号：上下文占用（当前窗口，非累计）、自动压缩次数与失败提示、模型降级（源 → 备用及是否成功）、本次运行用量（会话累计快照之差，不显示费用）、Goal 目标与状态（预算耗尽醒目提示）、todo 提醒次数与未完成条目。新增独立的凭据停用告警事件 `omp.credential_disabled`，只含 provider，同一进程内同一 provider 只告警一次。
- 补测计划评审“保存”“细化”：保存后 OMP 切到新会话，旧周期静默收束；细化在同一周期继续并按最终评审结果收束。不存在一直不收束的问题。

## v2.1.0 - 2026-10-24

本版本发布 OMP V2 通知（#28～#31）：OMP Hook 改为发送独立的 `omp.run_finished`／`omp.action_required`，带观察时间线，与 OpenCode 共用卡片。原生 OMP 18.12.6 的验收结果见 [OMP V2 验收记录](docs/omp-v2-acceptance.md#原生-omp-验收)。

### 升级须知

- **先升级服务端，再换客户端。** 先在 AstrBot 安装 v2.1.0 并确认插件已重载，再替换 OMP Hook。旧版服务端会以 HTTP 400 拒绝 V2 事件，客户端视为永久错误、不重试，这段时间的通知会丢失。新服务端继续接收旧 V1 Hook 与此前的 V2 测试 Hook，未升级的客户端仍可使用。
- **旧 V1 Hook 的结束事件现在显示为“未知”。** V1 的 `stop` 不能证明运行最终结束（可能还会续跑），因此不再显示为“已完成”；失败与中止仍按实际分类。换用新 Hook 后恢复为准确的完成状态。
- **图片前新增一行文字。** 每条图片通知前带 `【状态】会话名称`，待处理事件再追加摘要，与图片在同一条消息里。
- **待处理卡改版。** 顶部状态条显示等待类型与已运行时长，问题、审批工具、计划标题分块展示；纯文本与自定义模板不变。
- **已运行的 OMP 要重启才会加载新 Hook。** `/reload-plugins` 不会重载 Hook，替换文件后需要退出并重新启动 OMP 进程。
- **手动复制 Hook 的用户需要重新复制。** 从本仓库或 Release ZIP 的 `integrations/omp/webhook-notifier.ts` 重新复制到 OMP 的 Hook 目录（例如 `~/.omp/agent/hooks/post/`），并删除旧的 `onebot.ts` 等重复入口，避免同时加载两份。
- **请求超时默认改为 30 秒。** 按旧示例设置了 `OMP_SESSION_WEBHOOK_TIMEOUT_MS=5000` 的环境，需要调高该值或删除这个变量，否则图片通知会在送达后超时重试。

### 变更

- 修复 OMP Hook 默认超时过短：服务端同步渲染并投递，原生验收中单图约 10 秒、主卡加时间线附图约 20 秒，原默认 5 秒会在消息已送达后超时重试，只得到幂等回放。默认改为 30 秒（上限不变）；显式设置 `OMP_SESSION_WEBHOOK_TIMEOUT_MS=5000` 的环境需要调高或删除该变量。
- 修复 eval `agent()` 与 workpool 子代理被误报为“未知”／“已中止”：OMP 子代理以 `yield` 提交结果后结束，原生事件中最终 stopReason 分别为 `toolUse` 和 `aborted`（workpool 提交全部条目后由 OMP 中止后续回合）。Hook 现在把成功 `yield` 之后的这两种结尾计为完成，不再逐个单独提醒；`yield` 失败或未调用时保持原映射。
- 修复 OMP 计划评审一次产生“已中止＋待处理＋未知”三条通知：OMP 计划模式提交计划时先以 `agent_end(aborted)` 收束本轮，再打开评审，批准后切换到新会话执行。交互式根运行的中止现暂缓 150ms 发送；同一会话随即打开计划评审时不发“已中止”，待处理卡沿用原周期计时。批准后旧周期静默收束，由执行计划的新会话正常通知；取消评审时发一条“已中止”，等待区间记为已取消。其他中止只晚 150ms 送出，耗时仍按 `agent_end` 计。
- 图片通知附带一行文字 `【状态】会话名称`，待处理事件再追加摘要（如 `【待处理】PTZ: Add … · 等待问题回答`），至多 60 字符，超长只截会话名。文字与主卡在同一条消息里，手机通知栏、手表和会话列表预览不再只有“[图片]”。OneBot 平台显示为“文字＋图片”；QQ 官方 Bot 会把文字显示在图片之后，预览形如“[图片]【已完成】…”。不拆成两条发送，因为预览只显示最后一条，拆开后先发的文字会被“[图片]”盖住。未知状态不加前缀，缺会话名时用卡片标题；文字不受自定义模板影响，生成失败时照常只发图片。纯文本模式不变。
- `scripts/deploy_dev.py` 新增接收端验收 `--verify`（配合 `--probe`、`--skip-deploy`、`--update-baselines`）：向测试 Endpoint 发送固定内容冒烟，从接收账号读回后核对文字、文字与主卡同条、图片张数，并与 `tests/visual/baselines/` 中经 QQ 读回的线上渲染基准图逐像素比较。基准图首次取自 `v2.0.0-test.20261024.1002.caption-first`，在 `caption-inline` 版本上复验一致。
- 待处理图片卡改版（OMP 与 OpenCode 共用）：顶部黄色状态条显示“等待问题回答／等待工具审批／等待权限批准”等摘要和已运行时长，会话名作标题，项目、代理、模型与开始时间合为一行（与事件同日只显示时分秒）；问题、审批工具、计划标题和权限请求分块展示，选项带编号、描述，推荐项高亮。原字段表与“待处理”徽章仅在此类卡片中移除，剩余字段以短行保留；纯文本、自定义模板和其他卡片不变。
- 修复 OMP 名称与待处理内容缺失：会话名含相对路径（如 `Phowx/script`）时不再整体丢弃、退回匿名名称，只把 URL 和绝对／用户目录路径替换为 `…`；子代理会话没有标题时改用 OMP 任务名，不再显示“未命名子任务”。待处理提醒按 OpenCode 问题卡展示：ask 的题目、标题、编号选项及描述和推荐标记，审批的工具名，计划审阅的标题；答案、计划正文与审批原因仍不发送。服务端需先于客户端升级。
- OMP 卡片精简：空时间线不再展示；去掉成功卡固定说明、默认 main 代理、重复指标格与生成时间；完整观察不再附免责声明，只在缺口或截断时提示范围；实际 thinking 与 OpenCode 一样合并为 `模型(thinking)`，并移除会显示为方框的私用区图标字符。OpenCode／Markdown 展示不变。
- #31 OMP 共用卡片：根事件直接复用主卡、时间线布局及投递降级；公开实际 thinking 边界采样，变化后不伪造单一值。观察段／周期、四类交互与后台轨道、并集和缺口标签一致用于文本／图片；保留 OpenCode／Markdown 语义。完成离线 Chromium 表面与真实 HTTP 图片／降级检查，并在原生 OMP 18.12.6 与线上测试私聊中验收。
- 补齐模型名称隐私边界：provider 前缀不再掩盖绝对／用户目录／Windows 模型路径；客户端舍弃，V2 接收端拒绝，避免出站和卡片泄漏完整路径。
- #30 OMP 观察时间线：复用同一 Run／交互配对，公开 parent 身份＋owner scoped snapshot 关联已加载实例；按 cycle 区分 worker 复用，续跑段、后台等待和审批／计划／ask 分开计时。单调时钟、匿名有界组件、严格父图与边界校验，缺口／歧义／截断不补造历史；正常子代理完成聚合，异常仍提醒。
- #29 OMP V2：新增独立 `omp.run_finished` / `omp.action_required`，观察最终 `agent_end`、续跑／重试和审批／计划／ask 配对；匿名白名单、有界非阻塞发送、HTTP＋业务分类和稳定 ID 重试。保留 V1 接收，纠正失败／中止，缺最终证据为未知；仅 V2 规范成功参与短任务过滤。
- OMP 投递异常只记录固定失败分类，移除原始错误与 traceback；V1 标准化不再把完整路径、原始 ID 与 raw 元数据传入展示。OpenCode／Markdown 的投递契约不变。
- 本次实施与分层证据见 [OMP V2 验收记录](docs/omp-v2-acceptance.md)。真实 Hook 部署副本未改动，默认同步器保持停止并禁用；下述“本机部署切换”是此前基线的历史记录，不是当前同步状态。
- 新增 macOS 开发工具 `scripts/sync_omp_hook.py`：单文件、跨盘单向同步；提供安装、启停、状态、单次同步、显式备份覆盖及卸载命令，由用户级 LaunchAgent 管理主硬盘运行副本。不操作 Git、不执行 Hook、不进入 AstrBot 插件 ZIP。
- 源缺失或为空时保留部署副本；两次稳定观察后原子替换目标，目标独立修改／删除时持久化暂停，进程重启不清除冲突。显式覆盖前保留备份，卸载保留目标、配置、状态与备份。
- 同步器初次验收：26 项回归、全仓 Python 1226 项与 Ruff 通过；真实 macOS LaunchAgent 跨文件系统验收 13 项通过，临时实例已清理。该阶段未接管真实 OMP Hook，未验证 OMP 热加载、登录重启或物理磁盘拔插。
- 新增仓库维护的 `integrations/omp/webhook-notifier.ts`，以现用本地 `onebot.ts` 为基线，保留 `omp.session_stop` V1、`OMP_SESSION_*`／`ONEBOT_*` 配置和既有发送行为；不自动替换本机 Hook 或启用同步。
- OMP 客户端运行文件纳入后续插件 ZIP，Bun 测试不进入发布包；接入文档改用仓库维护入口，补充旧入口迁移、重复加载风险及既有隐私／HTTP 响应处理限制。
- OMP 维护基线验证：Bun 两客户端合计 293 项、Python 全仓 1226 项与 Ruff 通过；验证 ZIP 提取的 Hook 已完成本地真实 HTTP 发送及现有 OMP 解析／标准化 smoke。除维护注释外与本机原 Hook 一致；未执行 OMP 原生加载、线上 AstrBot 投递、登录重启或物理拔插验证。
- 本机部署切换：旧 `onebot.ts` 移出自动加载目录并保留校验一致的备份，新 `webhook-notifier.ts` 作为普通文件由默认 LaunchAgent 单向同步；实际检查进程运行、内容一致、状态 `in_sync` 且无冲突。此结果不代表现有 OMP Session 已热加载，也未新增线上投递验收。

---

## v2.0.0 - 2026-10-22

- **破坏性变更（#27）：** 默认 Provider 从 OMP 改为 Markdown，覆盖新建 Endpoint 和缺少 `provider` 字段的旧记录。显式保存为 `omp`、`opencode` 或 `markdown` 的 Endpoint 保持原协议；版本号升至 2.0 表示默认行为的兼容性边界，不改变 Webhook payload 版本或 Registry 格式版本。
- 整理多 Provider 定位：根文档、初始化说明与默认渲染使用通用通知表述，默认快速开始发送 `markdown.message`；OMP/OpenCode 接入示例显式指定对应 Provider。
- `token new` 未指定 `--provider` 时默认 `markdown`；仅成功创建私聊 Endpoint 或群待验证申请后在公开说明中提醒默认协议及 OMP/OpenCode 的显式参数要求，不将提醒混入独立 Token 消息。错误、不支持、缺值或重复的 flag 仍拒绝，不回退默认值。
- Provider 支持集合以运行时 `ProviderRegistry` 为权威，命令校验复用注册表，帮助选项按注册名称排序生成。显式 `omp`、`opencode`、`markdown` 保持各自原协议，Endpoint Provider 仍不可变。
- **迁移注意：** 旧记录只有 `provider` key 缺失才按 `markdown` 读取，显式空字符串或 `null` 仍非法；不会自动覆盖显式历史 Provider，也不按 payload 猜协议。依赖缺失字段的旧 OMP 调用需新建 `--provider omp` Endpoint 并更新 URL/Token；轮换 Token 不切换 Provider。
- 实现阶段本地验证：Python 1224 项测试、Ruff、默认创建、旧记录缺失字段加载、各 Provider 的 HTTP 投递与重放去重 smoke 通过；临时额外 Adapter 可被命令、帮助和 HTTP 路由接受，Chromium 已核对帮助卡片动态选项、默认说明与横向边界。
- 正式版本本地门禁：锁文件检查、frozen 同步、Ruff、前端 clean build、Python 1200 项（含实际 Chrome timeline 检查）及 Bun 279 项通过。测试数减少 24 项，原因是移除固定版本、文档措辞和 workflow 源文本断言；保留跨文件版本一致性及实际打包行为验证，开发包测试不再绑定当前版本字面量。
- 本地正式 ZIP：2,907,961 bytes，51 entries（50 个文件），单一插件根目录，版本三源一致；CRC、源码逐文件一致性与敏感/缓存排除检查通过。直接从 ZIP 导入 Markdown Adapter 并解析通知成功。SHA-256：`606ecded6c7f14e15b4ad8ef1b9565e7f95669bc8926c8a32e3d173d24db61b5`。此摘要仅对应本地产物，远端正式资产需另行核验。
- 测试包线上验证：默认 Markdown 私聊创建、协议提醒、Token 独立交付和帮助图片已通过截图核对；一次真实 Markdown 请求返回 HTTP 200、`code=0`、`delivered=true`、`rendered=true`、`render_mode=html_image`、`fallback_reason=null`。临时 Endpoint 已撤销并删除。
- 验证边界：线上 OMP/OpenCode 通知、群创建、缺字段旧记录迁移、通知图片接收端视觉确认和插件市场安装/更新未执行或未收集证据。测试包验证不替代远端正式 ZIP 的生产安装验证。

---

## v1.3.0 - 2026-08-19

- 修复 OpenCode Desktop 在会话元数据回退路径中的会话标题回退处理，使回退逻辑按现有安全会话标题契约选择可展示标题；本条只记录回退行为已修正，不把诊断信号夸大为已确认的 Desktop 内部根因。
- 新增通用 `markdown` provider 的受 endpoint Bearer 鉴权保护的信息推送能力：请求通过既有 endpoint 鉴权后，接收固定 `event: "markdown.message"` 的 JSON object；允许字段仅为 `event`、`id`、`title`、`markdown`、`target_alias`。`markdown` 必须为非空字符串，并受 32,768 字符与 64 KiB 限制；可选字段也有界校验，未知字段拒绝。内容由受限 Markdown parser 渲染为文本或 HTML，不进入 Jinja 执行。
- 增加 OpenCode 根会话上下文：子会话卡片保留当前会话，并显示“所属主会话”；完整 root-cycle 时间线显示“所属会话”。出站只传递安全清洗后的 `session.rootName`，父链只使用匿名 `parentRef` 且最多解析 16 层；父项缺失、出现环路或超过深度时省略，不传 ID、`parentID`、`rootRef`、路径或原始对象，旧 payload 继续向后兼容。
- 已完成的真实链路与针对性证据：Bun 279、Python 合并 532、Package 14、Chromium 静态渲染 smoke、测试 ZIP 结构检查；OpenCode Desktop 完全重启后 2 次 Question、2 次 Permission 及 `session_idle` 链路均返回 HTTP 200，用户目视确认显示正常。
- 完整 Release 门禁（本地）通过：`uv lock --check`、全仓 Ruff、前端 clean build、Bun 279 项与 Python 1191 项测试全部通过。本地正式 ZIP 为单一插件根目录、51 entries、2,902,505 bytes，三版本源为 `v1.3.0` / `1.3.0`，SHA-256 为 `4a22f4b91d111a0b33b763849cf72a6b2753de96ccd755fe342d429d47adb2e1`，forbidden=0。
- 正式发布证据：GitHub Actions run `32240193983` 的 attempt 2 成功；首轮仅因 Chrome CDP target 启动瞬态 `Connection refused` 失败，重跑通过。tag `v1.3.0` 精确指向提交 `9dadf96608ef4018d724d27059389db48ba33e97`；GitHub Release 为非 draft、`prerelease=false` 且为 Latest，发布于 `2026-08-19T10:00:25Z`。正式 ZIP 为 51 entries、2,902,505 bytes，远端 SHA-256 为 `1fba8ce3acb360246148c5b37dc057e67c4155d1e7c6383b5667d252ac224465`，Release notes 已覆盖本版本三项主要变更。
- 插件市场安装/更新路径与生产 AstrBot 使用远端正式资产的 smoke 仍待按实际结果验证；本节不将这两项写成已完成。

---

## v1.2.0 - 2026-08-10

- 自 `v1.1.0` 以来新增 OpenCode 元数据异常诊断、Subagent Timeline 覆盖统计与用户等待时间线，并将这三组向后兼容能力纳入 `v1.2.0` 稳定版，不破坏既有 V1 公共契约。
- anomaly 元数据异常诊断（`metadataDiagnostics=anomaly`）：全天低概率取证模式，专用于捕获 OpenCode Desktop 内部 session 被判为 root/unknown 并产生 fallback 卡片的问题。每阶段最多 32 条候选，按同一匿名 session + 相同安全 payload 去重；不增加任何 `session.get` / `session.messages` / HTTP 调用，只观测既有链路。日志为有界、匿名、fail-closed 的元数据诊断，不记录标题/正文、ID 值、路径、Token 或响应体。该模式不承诺已经找到 fallback 根因，只提供取证信号；采集后应恢复为 `off`。
- #25：Subagent Timeline 顶部覆盖统计：总任务时长使用 root busy→idle 周期的可靠 `durationMs`；子任务覆盖时长仅合并 timing quality 为 `observed`/`fallback` 且起止完整、并裁剪到 `[0, 总任务时长]` 的可靠区间，重叠或相邻区间按并集计时，覆盖率限制在 0～100%。数据不完整时只统计可靠完整区间，并将峰值与覆盖指标标为“已观测”；不对未覆盖区间作 root 或其他执行归因。统计区改为两行响应式网格布局，甘特图仍只展示 subagent。
- #26：统一用户等待时间线（`userWaitTimeline`）：OpenCode Client 采集 Question/Permission asked→resolved 的插件接收时间区间，以独立可选字段、严格隐私安全且向后兼容的 envelope 传递，并在同一张完整 root-cycle 甘特图中与 subagent 区间对齐展示。
  - 甘特图横轴改为完整 root 总时长：总任务时长可靠时横轴为 `0 → durationMs`，缺失时才回退到已观测最大终点并标记不完整；顶部固定“等待用户”轨道，Question 与 Permission 可区分；不单独绘制第二张图，不计入子任务数、峰值并发或 subagent 覆盖率。
  - interval 使用 `kind`、`result`、`intervalState`、root 相对 offset 与 duration；`complete` 区间参与并集计时，left/right censored 保留边界但不伪造 duration、不参与并集；等待区间与 subagent 可重叠，不推导主 agent 真实活跃时间。
  - 统一图顶部增加“未分类时间 / 占比”：总任务时长减去已知可靠并集（subagent 并集 + 等待并集）得出，观测受限时标注“至多/观测受限”，不把未观测时间归因给 root 或其他执行。
  - 只统计 root session 自身的等待；原始 session/request ID、正文、答案、pattern/target、URL 与 Token 一律不出站、不进日志、不渲染；request ID 仅用于去重/撤销。
  - 服务端先升级并重载，再部署新版 TS Client 并完全重启 OpenCode；旧服务端严格 allowlist 不接受新增 `userWaitTimeline`。
- 验证证据（正式发布前本地门禁与真实链路）：
  - `uv lock --check`、全仓 Ruff、前端 clean build 与 1123 项 Python 测试通过；Bun（TypeScript Client）272 项通过；真实 Chrome/CDP timeline geometry 与截图 smoke 5/5 通过。
  - 真实 Question/Permission、长等待场景与 T2I 渲染 smoke 已通过。
  - 本地正式 ZIP 为单一插件根目录、49 个文件、2,890,724 bytes，三版本源为 `v1.2.0` / `1.2.0`，SHA-256 为 `89fa8ebe3f9541c87b1d3f365d305e56061cf17ae40d6f75fe406852aed244b1`，未包含 `.git`、`.env`、auth/secrets、`node_modules` 或 `tmp`。
  - GitHub Actions dry-run、tag、GitHub Release、远端正式资产及 AstrBot 插件市场安装/更新路径仍须按实际结果分别留证，当前不宣称已完成。

---

## v1.1.0 - 2026-07-30

- 将 v1.1.0-rc.1 已验证范围定稿为 v1.1.0，不新增业务功能。
- RC 验证范围：
  - uv/Ruff Release CI、1018 Python tests、前端构建、正式 RC ZIP 契约均已通过。
  - 用户已将本地 rc-smoke 测试包部署到 AstrBot v4.26.7，最近 60 分钟约 45 个 request_id、13 次实际投递全部 message_sent；session_not_found、send_failed、4xx/5xx、重复投递均为 0。
  - 新增 delivery 日志字段在线生效，未发现凭据、payload、正文或完整 UMO 泄露。
- 稳定版发布前完整门禁通过 1019 项 Python 测试、全仓 Ruff、前端构建与 `v1.1.0` ZIP 三源版本校验。
- `v1.1.0` 作为稳定版，Release workflow 应计算 `prerelease=false`、`make_latest=true`。

---

## v1.1.0-rc.1 - 2026-07-29

- #11：Release Workflow 改为 uv 锁定环境与 Ruff lint 门禁：
  - 将 `actions/setup-python` + pip 迁移到 `astral-sh/setup-uv`（固定 SHA `c771a70e6277c0a99b617c7a806ffedaca235ff9` # v9.0.0），固定 uv 版本 `0.11.12`，Python 3.13，启用 uv cache。
  - `pyproject.toml` dev dependency group 新增 Ruff；`uv.lock` 同步更新。
  - 新增 `uv lock --check`、`uv sync --frozen --group dev`、`uv run --frozen ruff check .` 步骤。
  - `uv run --frozen` 替代裸 `python` 命令运行版本校验、测试、打包。
  - `workflow_dispatch` 始终为 dry-run：完整执行锁检查、Ruff、前端、测试、版本校验、打包和 artifact 上传，但不调用 `action-gh-release`。
  - 手动 dry-run checkout 当前触发 ref，候选 tag 仅用于版本契约和 artifact 命名，不要求预先创建 tag。
  - 正式 Release 仅在 `github.event_name == 'push'` 且 ref 为 v tag 时执行。
  - 不启用 `ruff format --check`，避免全仓格式化噪音。
  - `requirements.txt` 保留作为运行时依赖声明，Release Workflow 不再使用 pip。
  - 更新 `docs/release.md` 与 CI 契约测试。

- #24：全局 `min_completion_duration_seconds`（最短完成通知时长）：成功完成的 Webhook 事件耗时长低于阈值时跳过通知，减少短任务噪音。默认 15 秒；设为 0 关闭过滤恢复旧行为。有效范围 0–3600。仅在 `canonical completed` 状态参与过滤；failed、action_required、unknown 不参与；notification_mode 过滤在 duration 之前判断。task_duration_ms 仅来自 Provider 的可靠任务耗时（OMP round.durationMs / startedAt→endedAt 差值，OpenCode payload durationMs），不对外通过 to_dict() 暴露。通过 `admin config min-duration` 命令查询/设置/reset。保存失败时回滚内存值，Server 保持旧值。
- #13：完善 Webhook 投递日志上下文与凭据过滤：成功、session 不存在和发送异常日志可关联 `request_id`、Provider、Endpoint、目标别名、投递阶段和耗时；意外异常保留经过凭据过滤的完整 traceback。过滤 Endpoint Token、Bearer/Token 认证值、Authorization/Cookie、API key/password/secret、URL userinfo 和敏感查询参数，不新增 payload、Header、消息正文或完整目标 UMO 日志；不引入聊天诊断、错误缓冲或 debug 模式。
- 修复 OpenCode `timeoutMs=5000` 在服务端渲染/投递约 5 秒以上时触发 retry、造成同一权限通知重复投递的问题：服务端新增按 Envelope 顶层 `id` 的进程内 single-flight 幂等与发送边界追踪；示例 timeout 调整为 15000 毫秒。
- 冻结多 Provider 通知降噪：新增全局 `notification_mode`，默认 `focused` 只抑制成功完成的 OpenCode `subagent`；`all` 保持全部通知，unknown scope/status fail-open。`actionContentMode` 与该策略正交。
- OpenCode root `session_idle` 可选携带 `subagentTimeline`：记录相对 root busy→idle cycle 的匿名子任务图、时间质量、部分/截断状态与有界计数；不新增配置项，也不在其他 event 或 scope 携带。
- #22：默认渲染按执行复杂度展示子任务阶段：简单流程保持最多 8 项主卡；复杂流程使用同一 MessageChain 的独立横向甘特图，按任务数、名称与跨度在 1440～2400px 内动态布局，并完整展示 payload 中最多 64 项。名称自然折行，25～64 项自动切换紧凑密度；3000px 为软高度预算。附图强制使用完整页面与动态 viewport 高度，时间刻度使用覆盖全跨度的 nice step，并采用与白色通知报告协调的浅蓝灰视觉；缺失或附图失败时仍只保留安全主卡。
- #23：subagent timeline item 可选携带清洗后的 `model` 与 `modelVariant`，简单主卡、复杂甘特图、安全模板 view 和文本降级统一按 `agent · model(variant)` 展示；`default` variant 不追加括号，旧 payload 保持兼容。
- OpenCode V1 新增 `session.scope`（缺失兼容为 `unknown`），Client 通过既有 `session.get()` 判断 root/subagent，永不发送 `parentID`；服务端升级必须先于 Client 重启。
- #18：引入 Provider Adapter / Registry 与依赖注入边界，支持 `omp` 与 `opencode` provider，并保持 Endpoint provider 在创建后不可变。
- #19：增加 OpenCode Server Adapter，接收 V1 envelope，覆盖 `session_idle`、`session_error` 与 `permission_asked` 三类事件。
- #20：增加 OpenCode V1 Client Plugin，使用正确的 `default { id, server }` 与 `plugin` tuple，支持 env/file URL/Token 配置、状态机、timeout 和有限 retry。
- #21：收紧 OpenCode 字段白名单与隐私边界，匿名化 session ref、清洗 name fallback，并补齐 Bun/Python 契约测试、隔离 CLI Plugin Service smoke、Desktop 安全 SKIP 与中文集成文档。
- 本 RC 的发布前本地验证覆盖 Python 1018、Bun 221 和 OpenCode 1.18.4 CLI smoke；这不等同于已通过 RC ZIP 的 AstrBot WebUI 安装、Bot Endpoint 实际验证或 Desktop 端到端 smoke。
- 使用本 RC ZIP 的 AstrBot WebUI 手动安装、Bot Endpoint 验证和 Desktop 端到端 smoke 尚待本 RC 包验证，不能据此宣称已通过或已发布 `v1.1.0`。

---

## v1.0.0 - 2026-07-21

- 将首个稳定版公共契约定稿为 `v1.0.0`，覆盖配置与安全默认值、命令、Registry v2、Webhook HTTP 响应、平台支持边界、模板管理和可安装 ZIP 结构；不在 RC 基础上新增业务功能。
- 保留并确认 `v1.0.0-rc.1` 已完成的自动化测试、构建、打包、Release Workflow、真实平台与 Webhook 链路验证证据。
- 云端兼容验证采用卸载 v0.3.0 旧包后安装 `v1.0.0-rc.1`、同时保留原数据目录和配置数据的方式；该结果支持“卸载重装后的数据兼容性”，不等同于原位升级、在线更新或 AstrBot 插件市场一键更新验证。
- `v1.0.0` Git tag、GitHub Release 和正式 ZIP 已发布；正式 Release 使用 `prerelease=false`、`make_latest=true`。
- AstrBot 插件市场安装与更新路径列为正式版发布/上架后的检查项，当前不宣称已经通过。

---

## v1.0.0-rc.1 - 2026-07-20

- 冻结首个 1.0 Release Candidate 的公共契约范围，明确配置、命令、Registry v2、HTTP 响应、平台边界与可安装 ZIP 结构。
- 收敛版本与发布流程：SemVer tag/插件版本使用 `v1.0.0-rc.1`，Python 项目版本使用 PEP 440 等价形式 `1.0.0rc1`。
- 增加版本一致性、响应契约、打包与 Release workflow 的聚焦验证，为 RC 新装及 v0.3.0 数据兼容 smoke 做准备。后续云端实际执行的是保留数据目录/配置数据的卸载旧包后重装，不是原位升级或插件市场更新。
- 更新 PRD、FSD、发布指南与公共契约文档；本 RC 不宣称新增业务功能，最终 v1.0.0 仅在 smoke 通过后归档 MVP。

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
