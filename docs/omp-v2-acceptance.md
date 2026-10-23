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

- 原生 OMP：只验证实际版本，尚未在原生 producer 中执行上述生命周期、UI 或标准 eval 间接 ask；Bun 注入公开事件只是确定性回归，不冒充原生覆盖。
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

- 上述“批量／eval／worker”来自确定性公开事件序列，不是实际 task、eval 或 workpool 调度证明。原生 OMP 事件层尚未执行；实际二进制仍只验过 `18.12.6` 版本。
- 仅关联已加载且证据唯一的实例。快照为 owner scoped 有界可见记录，不能恢复全历史；未加载、跨模块副本、跨进程、旧事件缺 cycle 身份等情况不宣称精确归属或完整计时。
- #30 的规范字段和区间并集已验收；卡片入口、分类标签、真实渲染和附图降级由 #31 验证。线上目标消息仍未授权、未执行。
- #30 已达到 #31 本地展示集成的依赖基线；原生与线上验收未勾选，Issue 与总任务保持未完成。
