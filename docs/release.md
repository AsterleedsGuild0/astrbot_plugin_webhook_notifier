# 发布流程

本项目通过 GitHub Actions 构建插件 ZIP 并发布到 GitHub Release。当前发布目标为 `v2.1.0`，发布 OMP V2 通知；服务端需先于 OMP 客户端升级（见 CHANGELOG 升级须知）。本地准备与 `workflow_dispatch` dry-run 均不创建 tag 或 Release，只有推送 `v*` tag 才触发正式发布。

版本号与文档表示源码契约，远端发布完成状态以 [GitHub Releases](https://github.com/AsterleedsGuild0/astrbot_plugin_webhook_notifier/releases) 及对应 Actions run 为准。已发布 tag/asset 不覆盖、不重写；线上部署另行验收。

## OMP V2 测试包与切换门禁

OMP V2 随 `v2.1.0` 发布，原生 OMP 与线上测试私聊的验收结果见 [OMP V2 验收记录](omp-v2-acceptance.md#原生-omp-验收)；下方 v2.0.0 的历史部署证明不作为本次验收。开发维护源与真实自动加载入口保持隔离，切换真实客户端与恢复同步器需另行决定。

### 本地测试包

```bash
uv run --frozen python scripts/package_plugin.py --dev-version --test-label omp-v2
```

- 产物沿用 `dist/`，为单一 `astrbot_plugin_webhook_notifier/` 根目录；不改工作区正式版本号。
- 检查 ZIP 内 `metadata.yaml`、`main.py` 与 `pyproject.toml` 的规范版本等价；OMP／OpenCode 客户端、core、静态资源、模板、接入与公共契约齐全。测试、缓存、临时 HOME、凭据和同步器不进入安装包。
- 将 ZIP 解压到仓库既有 `tmp/` 下，再执行 `uv run --frozen python scripts/smoke_omp_hook.py --package <解压后的integrations/omp>`。这证明打包入口经真实回环 HTTP 可用，不证明 AstrBot WebUI 安装、原生 OMP 事件或线上目标消息。
- 当前构建版本、ZIP／运行文件哈希和提取 smoke 回执单独保存在 `tmp/omp-upgrade/31-package.json` 与 `31-package-http.json`；分层证据见 [OMP V2 验收记录](omp-v2-acceptance.md)。这些本地验收产物不随插件 ZIP 分发。

### 脚本部署到线上 AstrBot

`scripts/deploy_dev.py` 通过 Dashboard Open API（`/api/v1/plugins*`，依据 AstrBot v4.27.2）执行与 WebUI 相同的步骤：卸载现有插件 → 上传 ZIP → 轮询确认线上版本 → 向 OMP Endpoint 发送 4 条合成 V2 事件（简单完成、多代理时间线、失败、待处理 ask）并检查 `delivered=true`。

```bash
uv run --frozen python scripts/deploy_dev.py --env-file <凭据文件> --label omp-concise   # 新建测试包并部署
uv run --frozen python scripts/deploy_dev.py --env-file <凭据文件> --zip dist/<包>.zip   # 部署已有包
uv run --frozen python scripts/deploy_dev.py --env-file <凭据文件> --dry-run              # 只核对线上版本
```

- 凭据文件为 `KEY=VALUE`：`ASTRBOT_DASHBOARD_URL`、`ASTRBOT_API_KEY`（Dashboard 中创建、仅需 `plugin` scope）、`WEBHOOK_NOTIFIER_URL`、`WEBHOOK_NOTIFIER_TOKEN`。放在 Git 忽略的私有位置，不进入仓库。
- 卸载固定 `delete_config=false`、`delete_data=false`，Endpoint Registry 与插件配置保留。上传或加载失败时，自动回装 `tmp/deploy-dev/last-deployed.json` 记录的上次成功包并以非零退出。
- 冒烟卡片发送到凭据中的 Endpoint 目标；默认冒烟只验证 HTTP 与业务结果，卡片显示效果仍需在目标聊天中查看。AstrBot 升级后先用 `--dry-run` 和接口源码核对路由是否变化。

#### 接收端验收（`--verify`）

```bash
uv run --frozen python scripts/deploy_dev.py --env-file <Dashboard 凭据> --label <标签> \
  --verify <测试 Endpoint 凭据> --probe <接收端探测命令>       # 部署后验收
uv run --frozen python scripts/deploy_dev.py --skip-deploy --verify <…> --probe <…>   # 只验收线上现有版本
uv run --frozen python scripts/deploy_dev.py --skip-deploy --verify <…> --probe <…> --update-baselines  # 有意改版后更新基准
```

- `--verify` 代替默认冒烟：只读取该文件中的 `WEBHOOK_NOTIFIER_URL`／`WEBHOOK_NOTIFIER_TOKEN`，不使用环境变量里的生产 Endpoint，避免测试卡发往生产聊天。测试 Endpoint 应绑定一个不接入任何会自动回复的 AstrBot 平台的接收账号。
- 冒烟内容固定（会话名带 `[视觉基准]`，时间固定为 2026-01-01 08:00:30 CST），卡片逐像素确定。脚本按会话顺序读回消息并核对：每组文字与预期一字不差、文字与主卡在同一条消息（通知预览只显示最后一条，拆开发送会只剩“[图片]”）、图片张数（时间线用例为主卡加附图），再与 `tests/visual/baselines/<用例>-<序号>.png` 比较；超过 0.1% 像素差（通道差 > 16）即失败，并在接收图片旁写出差异图。
- 基准图来自线上 T2I 实际渲染、经 QQ 接收端读回的 PNG，不在本机生成，以避开字体差异。有意改版时先人工看过本次收到的图片，再用 `--update-baselines` 覆盖并在同一提交中更新基准图；不要为了让验收通过而更新基准图。
- `--probe` 是外部只读命令，接口为 `<probe> messages <since_rfc3339> <marker>`，输出 JSON：`ok`、`history_status`，以及按会话顺序排列的 `messages[]`（`marked`、命中标记时的 `text`、`segments[]` 中图片的 `format`／`width`／`height`／本地 `path`）。具体接收账号与读回方式属于部署环境，不在本仓库实现。

### 授权后的切换顺序

1. 备份目标服务端配置／插件和唯一 Hook 入口，记录版本、哈希、普通文件属性及权限；确认默认同步器仍停止、禁用。`in_sync` 是历史记录，不能替代 loaded 检查。
2. 先安装支持 V2 且仍接收 V1 的服务端，用专用测试 Endpoint 验证 HTTP 业务结果及实际目标消息；V2 新事件不得发向旧服务端。
3. 原生 OMP 使用隔离 HOME／配置、显式单一扩展入口、独立测试目标和已授权的测试凭据。暂停同步没有停用现有 Hook；禁止继承生产 URL／Token 或同时自动发现旧入口。
4. 分别核对正常结束、失败／中止、交互解决／取消、续跑／重试、task／eval／worker 多代理观察；先看实际公开事件，再核对通知。源码研究和合成回归不能代替这一层。
5. 通过上述门禁后，才在对应授权下切换真实客户端。是否启用同步须单独决定；没有恢复默认同步器的隐含步骤。

### 授权后的客户端回滚

1. 保持同步器停止、禁用；停止或退出此次切换涉及的 OMP 实例，避免已加载的 V2 Hook 继续发送。不能仅覆盖文件就认为旧模块已经卸载。
2. 核对预先保存的 V1 副本及目标唯一入口。对本次维护基线，保留副本的 SHA-256 为 `aaf06e753f9428eea0794dea2db586d4b38f2445480c8a49fbb37373461d9c99`；不得用正在开发的 V2 源文件充当回滚副本。
3. 仅在授权后，将已校验的 V1 文件恢复到同一个 `webhook-notifier.ts` 入口，保留普通文件及 `0600` 权限。示例中的变量必须指向已核对的备份和入口：

   ```bash
   shasum -a 256 "$VERIFIED_V1_BACKUP"
   install -m 0600 "$VERIFIED_V1_BACKUP" "$SINGLE_HOOK_ENTRY"
   cmp "$VERIFIED_V1_BACKUP" "$SINGLE_HOOK_ENTRY"
   ```

4. 确认自动发现目录没有第二份 `onebot.ts`／新旧重复入口，再以隔离测试目标启动对应实例并核对 V1 接收。开发源仍为 V2 时不要恢复自动同步，否则会再次覆盖回滚文件。
5. 如还需回滚服务端，先停止所有 V2 客户端，再执行独立授权的服务端回滚。不要只改 event／version 绕过拒绝，也不要换 ID 绕过幂等。只回滚客户端不会撤销新服务端的 V1 状态纠错：普通 stop 仍为 unknown，error／aborted 正确分类，不新增成功时长过滤。

本节是操作说明，不表示已执行安装、切换、回滚或恢复同步。

## v2.0.0 升级与验收

- 升级前备份插件配置与数据。显式 `omp`、`opencode`、`markdown` Endpoint 保持原协议。
- 新建 Endpoint 省略 `--provider` 默认 `markdown`；旧记录仅在缺少 `provider` key 时也按 Markdown 读取。依赖旧默认的 OMP 调用必须新建 `--provider omp` Endpoint 并更新 URL/Token，轮换 Token 不切换协议，不手工修改 Registry。
- Webhook payload 版本、Registry 格式版本不变；当前 OpenCode Client 不因本次默认值变更而要求更新。从更旧服务端升级时，仍需遵守历史 Client 字段的服务端兼容要求。
- 以 #27 中记录的本地 1224 项测试、Ruff、HTTP smoke 和 Chromium 帮助卡片检查为实现验收基线；正式版本另外执行下列发布门禁。
- 测试包 `v1.3.0-test.20261022.0936.provider-default` 基于 `8af7e2b`，已安装至线上 AstrBot。截图覆盖默认 Markdown 私聊创建、提醒、Token 独立交付和帮助图片。
- 2026-10-22 一次真实 Markdown 请求返回 HTTP 200、`code=0`、`delivered=true`、`rendered=true`、`render_mode=html_image`、`fallback_reason=null`；临时 Endpoint 已撤销并删除。此证据只覆盖测试包，不替代正式 ZIP 安装验收。
- 线上 OMP/OpenCode 真实通知、群创建、缺字段旧记录迁移和通知图片接收端视觉确认尚未验证；插件市场安装/更新也未验证，不将本地回归写成这些线上路径通过。

正式发布顺序：同步版本与更新日志 → 本地门禁和 ZIP 检查 → 提交并推送准备提交 → Actions dry-run 成功 → 将 `v2.0.0` tag 指向该提交并推送 → 核对正式 workflow、Release 标志及远端 ZIP。dry-run 后若修改源码，必须验证新提交后再打 tag。

---

## 发布前检查

1. 确认目标版本号一致：
   - `metadata.yaml` 和 `main.py @register` 使用 SemVer 风格插件版本；本轮为 `v2.1.0`。
   - `pyproject.toml` 使用规范 PEP 440 版本；本轮为 `2.1.0`，`uv.lock` 根项目版本同步更新。
   - `integrations/omp/package.json` 的 `version` 与插件版本等价。OMP marketplace 的安装与升级都按这个版本判断，Release workflow 会校验它与 tag 一致；`.omp-plugin/marketplace.json` 不写版本。
   - marketplace 读取默认分支，客户端改动只在发版时推送到 `main`，避免用户装到尚未发布的客户端。
   - 三者通过 PEP 440 规范化后必须等价，不要求 tag 去除 `v` 后与项目版本逐字相等。
   - `CHANGELOG.md` 顶部存在对应版本小节；本轮为 `## v2.1.0 - 2026-10-24`，包含升级须知（服务端先升级、V1 结束显示为未知、图片前文字、待处理卡改版、重启 OMP、重新复制 Hook、超时变量）。
   - 发布准备与 dry-run 不创建 `v2.1.0` tag；仅在门禁通过后推送 tag 触发正式 Release。
2. 维护本地锁定验证依赖：

   - `uv.lock` 必须随 `pyproject.toml` 的依赖声明一起维护并纳入提交。
   - 当前 `[dependency-groups].dev` 显式包含 `packaging`、PyYAML、pytest、pytest-asyncio、Pillow **和 Ruff**（自 #11 起引入）。
   - 依赖声明发生变化时先显式运行 `uv lock` 并审查 lockfile；普通发布验证不得隐式更新锁文件。

3. 本地推荐按锁文件同步环境并运行完整测试与 lint：

   ```bash
   uv sync --frozen --group dev
   uv run --frozen ruff check .
   uv run --frozen pytest
   ```

   同时执行 OpenCode Client 回归：

   ```bash
   bun test integrations/opencode/webhook-notifier.test.ts
   ```

4. 构建并验证 Plugin Page：

   ```bash
   npm ci --prefix frontend
   npm run build --prefix frontend
   uv run --frozen pytest tests/test_frontend_build.py
   ```

5. 本地生成带时分标识的测试包：

   ```bash
   uv run --frozen python scripts/package_plugin.py --dev-version
   ```

   测试包版本和文件名使用本地时间后缀 `-test.YYYYMMDD.HHMM`，例如
   `v1.1.0-rc.1-test.20260723.0905`。同一天多次打包时可直接按小时和分钟区分。ZIP 内 `pyproject.toml` 会使用等价的合法 PEP 440 dev 版本。该开发包与下面的固定 `v1.1.0-rc.1` RC ZIP 不同，不应混称。

   建议再添加本次测试用途标签，便于同时区分功能和生成时间：

   ```bash
   uv run --frozen python scripts/package_plugin.py --dev-version --test-label template-manager
   ```

   生成格式为 `-test.YYYYMMDD.HHMM.<label>`，例如
   `v1.1.0-rc.1-test.20260723.0905.template-manager`。标签仅允许英文字母、数字和连字符。

6. 本地验证正式发布包：

   ```bash
   uv run --frozen python scripts/package_plugin.py
   ```

---

## VSCode 本地打包

打开 VSCode Run and Debug 面板，可选择：

- `Package AstrBot plugin (test)`：生成带 `-test.YYYYMMDD.HHMM` 后缀的测试包。
- `Package AstrBot plugin (test flat legacy)`：生成 legacy flat 测试包。
- `Package AstrBot plugin (release)`：按 `metadata.yaml` 当前版本生成发布包。

---

## v1.1.0-rc.1 本地候选包

本 RC 的固定本地 WebUI 安装包使用 release-format 单一插件根目录：

```bash
uv run --frozen python scripts/package_plugin.py \
  --output dist/astrbot_plugin_webhook_notifier-v1.1.0-rc.1.zip
```

构建后必须检查版本三源、ZIP 根目录、OpenCode Plugin/配置示例、运行文件和敏感/缓存排除规则。该 ZIP 供用户通过 AstrBot WebUI 手动安装；安装、Bot Endpoint 和 Desktop 端到端 smoke 在本 RC 阶段均是待验证项。

---

## #11 CI 改造说明

本 Issue 将 Release Workflow 从 `actions/setup-python` + pip 迁移到 `astral-sh/setup-uv` + uv 锁定环境，并引入 Ruff lint 门禁。关键变更：

### CI 使用技术栈

| 组件 | 版本/标识 |
| --- | --- |
| setup-uv action | `c771a70e6277c0a99b617c7a806ffedaca235ff9` (对应的 tag `v9.0.0`) |
| uv 版本 | `0.11.12`（固定） |
| Python | `3.13` |
| Node.js | `20` |
| Ruff | dev group 依赖（当前 `0.16.0`） |

### 发布 Workflow 步骤顺序

1. **checkout** — `actions/checkout@v4`
2. **setup-uv** — `astral-sh/setup-uv`，固定 SHA、Python 3.13、启用 uv cache，依赖 glob 追踪 `pyproject.toml` + `uv.lock`
3. **setup-node** — `actions/setup-node@v4`，Node 20、npm cache
4. **`uv lock --check`** — 验证 lockfile 一致性
5. **`uv sync --frozen --group dev`** — 按锁文件安装全部依赖（含 dev 组 Ruff）
6. **`uv run --frozen ruff check .`** — lint 门禁（仅 F 规则，不含 format 检查）
7. **`npm ci --prefix frontend` + `npm run build --prefix frontend`** — 构建 Plugin Page
8. **版本校验** — `uv run --frozen python` 内联脚本校验 tag/metadata.yaml/main.py/pyproject.toml 三源一致性
9. **`uv run --frozen pytest`** — 运行完整测试套件
10. **`uv run --frozen python scripts/package_plugin.py`** — 构建插件 ZIP
11. **提取 release notes** — 从 CHANGELOG.md 提取对应 tag 的发布说明
12. **上传 Actions artifact** — 始终上传，dry-run 与正式发布均包含
13. **正式发布** — 仅当 `github.event_name == 'push'` 且 ref 为 `v*` tag 时调用 `softprops/action-gh-release`

### workflow_dispatch（手动触发）

- 始终为 **dry-run**：完整执行锁检查、Ruff、前端构建、测试、版本校验、打包和 artifact 上传。
- **不**创建 tag、不调用 `action-gh-release`。
- `tag` 输入仍用于版本校验和 artifact 命名，但不触发 Release 创建。

### 正式发布触发

- 仅 `push` v* tag 事件触发实际 GitHub Release。
- 不会有 `workflow_dispatch` 触发的正式发布。

### Ruff lint 门禁范围

- 本 Issue 只启用基于 `extend-select = ["F"]` 的 fatal lint 检查（pyflakes 规则）。
- **不启用** `ruff format --check`，避免全仓格式化噪音。
- 后续可追加规则而不影响既有的 CI 流程。

---

## 既有 v1.0.0 稳定版发布

推送版本 tag 会触发 `.github/workflows/release.yml`。`v1.0.0` 已是既有稳定版；下列命令仅保留为历史流程示意，不是本轮操作：

```bash
git tag v1.0.0
git push origin v1.0.0
```

正式发布时 tag 必须指向已经通过完整验证、版本三源均为目标版本的提交；上述 `v1.0.0` 命令不是本轮目标。

---

## 手动触发

如果需要在 CI 中演练完整工作流而不发布，在 GitHub Actions 页面手动运行 `Release` workflow，填写目标版本 tag。手动触发始终为 dry-run，不会创建 Release。Artifact 名称会包含目标 tag，例如 `plugin-release-v2.0.0`。下载后可用于安装测试，不能据此宣称正式 Release 已发布。

---

## 历史记录：v1.3.0 正式版发布

### 发布前门槛

1. `metadata.yaml`、`main.py @register`、`pyproject.toml` 和 `CHANGELOG.md` 对应 `v1.3.0` / `1.3.0`，且规范化后一致。
2. 完整 Python 测试、Bun 测试、前端 clean build/专项测试、浏览器 timeline smoke、版本与 package contract、正式 ZIP 构建全部通过（含 Ruff lint）。
3. 正式 ZIP 使用单一插件根目录，包含运行源码、OpenCode Plugin、配置示例和必要文档，不包含 `.git`、`.env`、auth/secrets、缓存、`node_modules` 或临时文件。
4. 本轮已完成的针对性证据可以记录为：Bun 279、Python 合并 532、Package 14、Chromium 静态渲染 smoke、测试 ZIP 结构检查，以及 OpenCode Desktop 完全重启后的 2 次 Question、2 次 Permission 与 `session_idle` 链路均 HTTP 200；用户已目视确认显示正常。该证据不替代完整 Release 门禁或正式 ZIP 的发布后复核。
5. 服务端与 Client 的兼容顺序必须明确：先升级并重载 AstrBot 服务端，再部署并完全重启 OpenCode Client。不得在旧服务端 strict allowlist 上先启用含 `session.rootName` 或 `userWaitTimeline` 的 Client。
6. 记录本地正式 ZIP 的 SHA256、大小、文件数和顶层摘要；不得把本地验证写成 GitHub Release、远端资产或插件市场验证。

### 本轮本地门禁证据

- 2026-08-19 已通过 `uv lock --check`、frozen sync、全仓 Ruff 与前端 clean build。
- Bun OpenCode Client 279 项、全仓 Python 1191 项测试通过；版本一致性和 package contract 包含在 Python 门禁中。
- 正式 ZIP 应保持单一插件根目录、三版本源一致且 forbidden=0；最终 size、entries 与 SHA-256 在包生成后记录到不进入 ZIP 的更新日志及发布验证结果中，避免包内文档自引用自身摘要。
- 上述证据覆盖本地源码与正式包；GitHub Actions、tag、Release 与远端 ZIP 已在发布后核对，插件市场路径与生产 AstrBot 使用远端正式资产的 smoke 仍待验证。

### 发布后检查

1. 已完成：GitHub Actions run `32240193983` attempt 2 成功；首轮仅因 Chrome CDP target 启动瞬态 `Connection refused` 失败，重跑通过。tag 指向预期提交，Release 非 draft、`prerelease=false` 且为 Latest，发布于 `2026-08-19T10:00:25Z`，详见 [v1.3.0 Release](https://github.com/AsterleedsGuild0/astrbot_plugin_webhook_notifier/releases/tag/v1.3.0)。
2. 已完成：正式 ZIP `astrbot_plugin_webhook_notifier-v1.3.0.zip` 为 2,902,505 bytes、51 entries；远端 SHA-256 为 `1fba8ce3acb360246148c5b37dc057e67c4155d1e7c6383b5667d252ac224465`，单一插件根目录、版本三源和包内容契约通过，forbidden=0。远端与本地 ZIP 的 size、entries、entry 清单及全部未压缩文件内容一致；SHA 差异仅来自 ZIP entry metadata。
3. 待完成：使用远端正式资产复核 AstrBot 新装链路和核心 Webhook 行为；生产环境 smoke 尚未执行。
4. 待完成：正式版发布后的 AstrBot 插件市场上架、市场搜索安装、从已安装版本触发的一键更新/在线更新，以及更新后的数据与配置行为。
5. 市场更新验证完成前，文档和发布说明不得声称该路径已通过；若市场机制实际采用重装，也应按观察到的真实行为记录，不推断为原位升级。

市场更新验证属于发布后检查；`v1.3.0` 的 tag、Release、正式 ZIP 已按实际结果留证，市场安装/更新路径与生产正式资产 smoke 仍须按实际结果分别留证。

---

## 手动兜底发布

如果 Actions 不可用，且已经获得明确的远端发布授权，可以使用 GitHub CLI 手动发布目标版本。以下仅为流程示意；优先修复并使用现有 Actions 流程，不自动切换兜底：

```bash
TARGET_TAG=v2.0.0
uv sync --frozen --group dev
uv run --frozen ruff check .
uv run --frozen pytest
npm ci --prefix frontend
npm run build --prefix frontend
uv run --frozen python scripts/package_plugin.py
gh release create "$TARGET_TAG" \
  "dist/astrbot_plugin_webhook_notifier-${TARGET_TAG}.zip" \
  --target main \
  --title "$TARGET_TAG" \
  --notes-file tmp/release-notes.md
```

手动兜底同样必须满足发布前门槛；稳定版应确认 `prerelease=false`、`make_latest=true`。已存在的 tag 必须核对其提交，不自动覆盖历史 tag 或资产。
