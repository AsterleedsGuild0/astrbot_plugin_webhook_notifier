# Registry platform_id 离线 Rebind Runbook

## 适用场景与边界

本 runbook 用于同一 AstrBot adapter 实例发生 `platform_id` 变更时，把 Registry v2 中受管理记录迁移到新 scope。例如两个 adapter 都允许创建同名 endpoint，但其中一个实例重建后 `platform_id` 改变；此时不能按 adapter 名称猜测归属，必须明确提供旧、新 `platform_id`。

- helper 仅处理 Registry v2 的 `managed records`，不会认领、移动或枚举 quarantine。
- `dry-run` 是零写入只读操作，技术上可在线执行，但仍建议先停服以得到稳定快照。
- `execute` 与 `rollback` 只能在 AstrBot 和插件完全停止后运行，必须显式提供 `--confirm-offline`。helper 不会自动停止服务。
- Registry 内的 `threading.RLock` 只协调插件单进程内线程，不能协调 helper 独立进程。
- rebind 保持 endpoint path、Token hash、算法、创建/撤销时间和生命周期字段不变；只修改选中记录的 record key、`owner_platform_id` 和所有 target UMO 的 platform 前缀。
- execute 与 rollback 都永久清空全部 pending，并把全部 `pending_verification` 记录（包括未选中记录）改为 `expired`；rollback 不会复活 pending。

---

## 命令契约

以下示例中的路径和 ID 均为占位符，不要把真实 Token、hash、owner、完整 UMO 写入命令记录或工单。

```bash
python scripts/rebind_platform_id.py \
  --registry "<DATA_DIR>/webhook_tokens.json" \
  --source-platform-id "<OLD_PLATFORM_ID>" \
  --destination-platform-id "<NEW_PLATFORM_ID>" \
  --dry-run
```

可选精确 owner selector：

```bash
python scripts/rebind_platform_id.py \
  --registry "<DATA_DIR>/webhook_tokens.json" \
  --source-platform-id "<OLD_PLATFORM_ID>" \
  --destination-platform-id "<NEW_PLATFORM_ID>" \
  --owner-user-id "<OWNER_USER_ID>" \
  --dry-run
```

执行：

```bash
python scripts/rebind_platform_id.py \
  --registry "<DATA_DIR>/webhook_tokens.json" \
  --source-platform-id "<OLD_PLATFORM_ID>" \
  --destination-platform-id "<NEW_PLATFORM_ID>" \
  --execute \
  --confirm-offline \
  --manifest "<SAFE_AUDIT_DIR>/rebind-audit.json"
```

回滚：

```bash
python scripts/rebind_platform_id.py \
  --registry "<DATA_DIR>/webhook_tokens.json" \
  --rollback-manifest "<SAFE_AUDIT_DIR>/rebind-audit.json" \
  --confirm-offline \
  --manifest "<SAFE_AUDIT_DIR>/rollback-audit.json"
```

成功返回码为 `0`；参数、校验、digest guard 或持久化失败返回 `2`。错误信息不会输出 owner、path、完整 UMO、Token 或 Token hash。

---

## 标准操作流程

### 1. 备份与停服确认

1. 确认 Registry 当前是 canonical `version: 2`。
2. 记录 AstrBot 与插件的停止窗口并停止服务，确认没有 adapter 事件处理和 Webhook 写入。
3. 在受限目录保留额外运维备份；不要修改 helper 自动生成的 `0600` backup。
4. 确认 source 与 destination 非空且不同；如使用 owner selector，必须是精确非空值。

### 2. Dry-run

运行 `--dry-run`，核对以下脱敏字段：

- `selected_count` 是否符合预期。
- `total_managed_count` 是否合理。
- `pending_expired_count` 是否已纳入维护窗口影响评估。
- `pre_sha256` 是否在执行前保持不变。
- `selected_post_key_fingerprints` 数量是否等于选择数量。

dry-run 不创建 backup、manifest，不修改 Registry 内容或 mtime。

### 3. Execute

停服状态下运行 `--execute --confirm-offline`。helper 会在一次写事务前完成全部校验：选择范围、destination key、全局 path、target UMO 前缀、Registry version/canonical 和 pending 不变量。任一校验失败时 Registry 零写入。

校验成功后，事务顺序固定为：在内存生成 candidate 与 post digest，复核 Registry pre digest，生成 durable `0600` backup，写入 durable `state: prepared` manifest，最后才通过临时文件、file fsync 和原子 replace 提交 Registry。manifest 写入失败时 Registry 保持 pre digest；Registry 写入失败时 prepared manifest 可以保留，但 Registry 仍保持 pre digest。不要直接用 backup 覆盖 Registry。

Registry 原子 replace 成功后的 parent fsync 采用 best-effort 语义：若该 fsync 失败，Registry 已视为提交，命令仍返回 `0`、`changed: true`，并在 `warnings` 中返回 `registry_parent_fsync_failed`。此时 durable prepared manifest 已先存在，应按其 post digest 立即核验 Registry，并在继续操作前人工确认底层文件系统状态。

### 4. 验证

1. 再次运行相同 source/destination 的 dry-run，应返回选择范围为空；这是已移动完成的预期结果。
2. 加载 Registry，确认新 `platform_id` 下可 list、rotate、revoke，旧 `platform_id` 下返回不存在。
3. 使用受控测试请求确认原 endpoint path 与旧 Token 在 rotate 前仍可鉴权。
4. 确认所有 pending 已清空，原 `pending_verification` 记录均为 `expired`。
5. 确认 Registry 可重复 reload，文件保持 canonical。

### 5. Rollback 条件

prepared manifest 本身就是 rollback 的完整 intention，不依赖 execute 后第二次 audit 更新。rollback 先比较当前 Registry digest：

- 等于 manifest `post_sha256`：execute 已提交，可继续安全 rollback。
- 等于 manifest `pre_sha256`：execute 未提交，rollback 安全拒绝，不改 Registry。
- 两者都不等：Registry 后续已变化或状态未知，digest guard 安全拒绝。

确认 execute 已提交后，只有同时满足以下条件才允许 rollback：

- 当前 Registry SHA256 精确等于 execute manifest 的 `post_sha256`。
- manifest 中 fingerprint 定位到的 record 集合完整且唯一。
- 逆向 destination scope 不存在 key 冲突。
- 所有选中 target UMO 前缀仍精确等于 execute destination。

若 execute 后发生 rotate、revoke 或任何 Registry 写入，digest guard 会拒绝 rollback。此时不要覆盖 backup，应先停止并人工评估新的前向修复方案。

rollback 使用相同的 manifest-before-commit 顺序：先在内存生成逆向 candidate，生成新的 durable backup，再写入新的 durable `state: prepared` rollback manifest，最后提交逆向 Registry。rollback manifest 写入失败时 Registry 不变；Registry 写入失败时 manifest 可保留且 Registry 仍为 rollback `pre_sha256`。replace 后 parent fsync 失败同样按已提交 warning 处理。rollback 保留当前 path、Token、status 和时间字段，pending 仍为空且 expired 状态不会恢复。

### 6. 恢复服务

1. 完成 execute 或 rollback 验证后再启动 AstrBot 与插件。
2. 检查插件 Registry 加载日志和 endpoint 数量，不输出或复制敏感字段。
3. 对新 platform scope 做最小 list/鉴权 smoke test。
4. 保留 execute manifest、rollback audit 和 backup 的访问控制；按运维保留策略归档。

---

## 脱敏 Audit Schema

execute manifest 与 rollback manifest 使用 `audit_version: 1`，包含：

- `operation`、`state: prepared`、`created_at`。`prepared` 表示 intention 已 durable，不单独宣称 Registry 是否提交；提交状态必须由 pre/post digest 判断。
- source/destination `platform_id`。
- `selected_count`、`total_managed_count`、`pending_expired_count`。
- `pre_sha256`、`post_sha256`。
- `backup_file`：仅保存自动生成的脱敏文件名。
- `selected_post_key_fingerprints`：选中 post-key 的不可逆 SHA256。
- rollback 额外包含 `rebind_manifest_sha256`。

Audit 不包含 owner 原值、endpoint path、完整 UMO、Token 明文、Token hash 或 server secret。
