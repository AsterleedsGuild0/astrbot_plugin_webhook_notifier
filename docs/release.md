# 发布流程

本项目通过 GitHub Actions 自动构建插件 ZIP，并发布到 GitHub Release。`v1.0.0` 是现有稳定版；当前源码准备的是尚未发布的 `v1.1.0-rc.1` 候选。本文档的本地准备步骤不创建 tag、GitHub Release 或远端资产。Workflow 使用 `setup-python`、pip 与 `python` 命令；本地推荐另用 uv 锁定环境验证，两者现阶段不是同一套依赖安装路径。

通知降噪候选发布还必须遵循兼容部署顺序：先升级并重载 AstrBot 服务端，再部署并完全重启 OpenCode Client。旧服务端的严格 allowlist 不接受新增的 `session.scope`。

---

## 发布前检查

1. 确认目标版本号一致：
   - `metadata.yaml` 和 `main.py @register` 使用 SemVer 风格插件版本，例如本 RC 的 `v1.1.0-rc.1`。
   - `pyproject.toml` 使用规范 PEP 440 版本，例如本 RC 的 `1.1.0rc1`。
   - 三者通过 PEP 440 规范化后必须等价，不要求 tag 去除 `v` 后与项目版本逐字相等。
   - `CHANGELOG.md` 顶部存在对应版本小节；本 RC 应为 `## v1.1.0-rc.1 - 2026-07-23`。
   - 本地 RC 准备不创建 `v1.1.0-rc.1` tag，也不创建或更新远端 Release。
2. 维护本地锁定验证依赖：

   - `uv.lock` 必须随 `pyproject.toml` 的依赖声明一起维护并纳入提交。
   - 当前 `[dependency-groups].dev` 显式包含 `packaging`、PyYAML、pytest、pytest-asyncio 与 Pillow，不包含 Ruff。
   - 依赖声明发生变化时先显式运行 `uv lock` 并审查 lockfile；普通发布验证不得隐式更新锁文件。

3. 本地推荐按锁文件同步环境并运行完整测试：

```bash
uv sync --frozen --group dev
uv run --frozen pytest
```

1. 构建并验证 Plugin Page：

```bash
npm ci --prefix frontend
npm run build --prefix frontend
uv run --frozen pytest tests/test_frontend_build.py
```

1. 本地生成带时分标识的测试包：

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

1. 本地验证正式发布包：

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

## 既有 v1.0.0 稳定版发布

推送版本 tag 会触发 `.github/workflows/release.yml`。`v1.0.0` 已是既有稳定版；下列命令仅保留为历史流程示意，不是本轮操作：

```bash
git tag v1.0.0
git push origin v1.0.0
```

正式发布时 tag 必须指向已经通过完整验证、版本三源均为目标版本的提交。本轮不执行任何 tag、push 或远端 Release 操作。

当前 `.github/workflows/release.yml` 在创建 Release 前实际执行以下步骤：

- 使用 `actions/setup-python@v5` 配置 Python 3.13 和 pip cache，并使用 `actions/setup-node@v4` 配置 Node.js 20。
- 运行 `python -m pip install --upgrade pip`，再从 `requirements.txt` 并附加 `packaging pyyaml pytest pytest-asyncio pillow` 安装依赖；`packaging` 用于 PEP 440 版本比较，Pillow 用于 HTML 图片渲染与裁剪测试。
- 使用 `npm ci --prefix frontend` 和 `npm run build --prefix frontend` 重建 Plugin Page。
- 校验 tag、`metadata.yaml`、`main.py @register` 与 `pyproject.toml` 的版本按 PEP 440 规范化后等价。
- 运行 `python -m pytest`。
- 运行 `python scripts/package_plugin.py` 生成 `dist/*.zip`。
- 从 `CHANGELOG.md` 提取对应 tag 的发布说明。
- 根据 tag 的预发布段动态设置 Release：稳定版为 `prerelease=false`、`make_latest=true`；RC 版本为 `prerelease=true`、`make_latest=false`。
- 创建或更新 GitHub Release，并上传插件 ZIP。

当前 CI 尚未使用 `uv.lock`，也没有 Ruff 门禁；将正式 CI 改为 uv 锁定安装属于后续改造项。在该改造完成前，不应把本地 uv 验证描述为现有 Release Workflow 已执行的步骤。若本地另行运行 Ruff，应视为独立可选检查，不属于当前 dev group 或发布 Workflow。

---

## 手动触发

如果目标 tag 已由授权发布流程创建，也可以在 GitHub Actions 页面手动运行 `Release` workflow，并填写目标版本。`workflow_dispatch` 与 tag push 使用同一版本校验、CHANGELOG 提取和 prerelease/latest 判定；手动触发不能绕过版本一致性和发布门槛。本轮不执行手动触发。

---

## v1.1.0-rc.1 发布门槛

### 发布前门槛

1. `metadata.yaml`、`main.py @register`、`pyproject.toml` 和 `CHANGELOG.md` 对应 `v1.1.0-rc.1` / `1.1.0rc1`，且规范化后一致。
2. 完整 Python 测试、Bun 测试、CLI smoke、前端 clean build/专项测试、版本与 package contract、RC ZIP 构建全部通过。
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

市场更新验证属于发布后检查；`v1.1.0` 是否成为稳定版以及市场安装/更新路径，必须在后续授权发布后单独留证。

---

## 手动兜底发布

如果 Actions 不可用，且已经获得明确的远端发布授权，可以使用 GitHub CLI 手动发布目标版本。以下仅为流程示意，本轮不执行：

```bash
TARGET_TAG=v1.1.0-rc.1
uv sync --frozen --group dev
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
