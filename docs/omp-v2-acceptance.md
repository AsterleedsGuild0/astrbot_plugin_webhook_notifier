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
