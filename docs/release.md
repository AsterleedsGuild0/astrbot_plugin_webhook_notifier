# 发布流程

本项目通过 GitHub Actions 自动构建插件 ZIP，并发布到 GitHub Release。`v1.1.0` 是当前稳定版。本文档的本地准备步骤不创建 tag、GitHub Release 或远端资产。

`v1.1.0` 发布与部署必须遵循兼容顺序：先升级并重载 AstrBot 服务端，再部署并完全重启 OpenCode Client。旧服务端的严格 allowlist 不接受新增的 `session.scope`。

---

## 发布前检查

1. 确认目标版本号一致：
   - `metadata.yaml` 和 `main.py @register` 使用 SemVer 风格插件版本；本轮稳定版为 `v1.1.0`。
   - `pyproject.toml` 使用规范 PEP 440 版本；本轮稳定版为 `1.1.0`。
   - 三者通过 PEP 440 规范化后必须等价，不要求 tag 去除 `v` 后与项目版本逐字相等。
   - `CHANGELOG.md` 顶部存在对应版本小节；本轮应为 `## v1.1.0 - 2026-07-30`。
   - 本地准备与 dry-run 不创建 `v1.1.0` tag，也不创建或更新远端 Release。
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

正式发布时 tag 必须指向已经通过完整验证、版本三源均为目标版本的提交。本轮不执行任何 tag、push 或远端 Release 操作。

---

## 手动触发

如果需要在 CI 中演练完整工作流而不发布，在 GitHub Actions 页面手动运行 `Release` workflow，填写目标版本 tag。手动触发始终为 dry-run，不会创建 Release。Artifact 名称会包含目标 tag，例如 `plugin-release-v1.1.0-rc.1`。下载后可直接用于本地安装测试。

---

## v1.1.0-rc.1 发布门槛

### 发布前门槛

1. `metadata.yaml`、`main.py @register`、`pyproject.toml` 和 `CHANGELOG.md` 对应 `v1.1.0-rc.1` / `1.1.0rc1`，且规范化后一致。
2. 完整 Python 测试、Bun 测试、CLI smoke、前端 clean build/专项测试、版本与 package contract、RC ZIP 构建全部通过（含 Ruff lint）。
3. RC ZIP 使用单一插件根目录，包含运行源码、OpenCode Plugin、配置示例和必要文档，不包含 `.git`、`.env`、auth/secrets、缓存、`node_modules` 或临时文件。
4. AstrBot WebUI 手动安装、Bot Endpoint 验证和 Desktop 端到端 smoke 必须按实际执行结果记录；本 RC 准备阶段不得把它们写成已通过。
5. 已完成的云端兼容验证必须准确表述为：卸载 v0.3.0 旧包后安装 `v1.0.0-rc.1`，同时保留原数据目录与配置数据。该结果支持卸载重装后的数据兼容性，不支持原位升级、在线更新或市场一键更新结论。
6. 记录本地 ZIP 的 SHA256、大小、文件数和顶层摘要；用户安装前不得把本地验证写成远端发布或市场验证。

### 发布后检查

1. 核对 GitHub Actions 成功，tag 指向预期提交，Release 非 draft、`prerelease=false` 且为 Latest。
2. 核对正式 ZIP 文件名、SHA256、单一插件根目录、版本三源和包内容契约。
3. 使用远端正式资产复核新装链路和核心 Webhook 行为。
4. 正式版发布并在 AstrBot 插件市场上架后，验证市场搜索安装、从已安装版本触发的一键更新/在线更新，以及更新后的数据与配置行为。
5. 市场更新验证完成前，文档和发布说明不得声称该路径已通过；若市场机制实际采用重装，也应按观察到的真实行为记录，不推断为原位升级。

市场更新验证属于发布后检查；`v1.1.0` 的 tag、Release、正式 ZIP 以及市场安装/更新路径必须按实际结果分别留证。

---

## 手动兜底发布

如果 Actions 不可用，且已经获得明确的远端发布授权，可以使用 GitHub CLI 手动发布目标版本。以下仅为流程示意，本轮不执行：

```bash
TARGET_TAG=v1.1.0
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

手动兜底同样只能在发布前门槛全部满足后执行；RC 应确认 `prerelease=true`、`make_latest=false`。当前尚未执行该命令。
