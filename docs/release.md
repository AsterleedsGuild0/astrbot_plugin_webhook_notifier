# 发布流程

本项目通过 GitHub Actions 自动构建插件 ZIP，并发布到 GitHub Release。当前文档按稳定版 `v1.0.0` Final 准备，但 `v1.0.0` Git tag、GitHub Release 与正式 ZIP 尚未创建；本轮仅更新文档，不代表代码版本字段或远端资产已经完成。Workflow 使用 `setup-python`、pip 与 `python` 命令；本地推荐另用 uv 锁定环境验证，两者现阶段不是同一套依赖安装路径。

---

## 发布前检查

1. 确认版本号一致：
   - 正式发布提交中的 `metadata.yaml` 和 `main.py @register` 必须使用 SemVer 风格插件版本 `v1.0.0`。
   - 正式发布提交中的 `pyproject.toml` 必须使用规范 PEP 440 版本 `1.0.0`。
   - 三者通过 PEP 440 规范化后必须等价，不要求 tag 去除 `v` 后与项目版本逐字相等。
   - `CHANGELOG.md` 顶部存在对应 `## v1.0.0 - 2026-07-21` 小节，并保留 RC 历史条目。
   - 当前仓库若仍显示 RC 版本，必须在后续代码发布准备中完成版本更新并重新验证；不得直接创建 `v1.0.0` tag。
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
`v1.0.0-test.20260721.0905`。同一天多次打包时可直接按小时和分钟区分。ZIP 内 `pyproject.toml` 会使用等价的合法 PEP 440 dev 版本。只有源文件版本更新为 `v1.0.0` 后，才可把该产物作为 Final 测试包。

建议再添加本次测试用途标签，便于同时区分功能和生成时间：

```bash
uv run --frozen python scripts/package_plugin.py --dev-version --test-label template-manager
```

生成格式为 `-test.YYYYMMDD.HHMM.<label>`，例如
`v1.0.0-test.20260721.0905.template-manager`。标签仅允许英文字母、数字和连字符。

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

## v1.0.0 Final 发布

推送版本 tag 会触发 `.github/workflows/release.yml`：

```bash
git tag v1.0.0
git push origin v1.0.0
```

上述命令仅表示正式发布时的操作，不是本轮已执行事项。创建 tag 前必须满足发布前门槛，且 tag 必须指向已经通过完整验证、版本三源均为 `v1.0.0` 的提交。

当前 `.github/workflows/release.yml` 在创建 Release 前实际执行以下步骤：

- 使用 `actions/setup-python@v5` 配置 Python 3.13 和 pip cache，并使用 `actions/setup-node@v4` 配置 Node.js 20。
- 运行 `python -m pip install --upgrade pip`，再从 `requirements.txt` 并附加 `packaging pyyaml pytest pytest-asyncio pillow` 安装依赖；`packaging` 用于 PEP 440 版本比较，Pillow 用于 HTML 图片渲染与裁剪测试。
- 使用 `npm ci --prefix frontend` 和 `npm run build --prefix frontend` 重建 Plugin Page。
- 校验 tag、`metadata.yaml`、`main.py @register` 与 `pyproject.toml` 的版本按 PEP 440 规范化后等价。
- 运行 `python -m pytest`。
- 运行 `python scripts/package_plugin.py` 生成 `dist/*.zip`。
- 从 `CHANGELOG.md` 提取对应 tag 的发布说明。
- 根据 tag 的预发布段动态设置 Release：`v1.0.0` 必须为 `prerelease=false`、`make_latest=true`；RC 历史版本保持 `prerelease=true`、`make_latest=false`。
- 创建或更新 GitHub Release，并上传插件 ZIP。

当前 CI 尚未使用 `uv.lock`，也没有 Ruff 门禁；将正式 CI 改为 uv 锁定安装属于后续改造项。在该改造完成前，不应把本地 uv 验证描述为现有 Release Workflow 已执行的步骤。若本地另行运行 Ruff，应视为独立可选检查，不属于当前 dev group 或发布 Workflow。

---

## 手动触发

如果 tag 已存在，也可以在 GitHub Actions 页面手动运行 `Release` workflow，并填写 `v1.0.0`。`workflow_dispatch` 与 tag push 使用同一版本校验、CHANGELOG 提取和 prerelease/latest 判定；手动触发不能绕过版本一致性和发布门槛。

---

## v1.0.0 发布门槛

### 发布前门槛

1. 版本三源、`CHANGELOG.md` 与目标 tag 均为 `v1.0.0` / `1.0.0`，且规范化后一致。
2. 完整 Python 测试、前端 clean build/专项测试、版本与 package contract、正式 ZIP 构建全部通过。
3. 在干净 AstrBot 环境中使用待发布 ZIP 新装，验证顶层插件目录、配置加载、命令、Endpoint、HTTP 鉴权和通知链路。
4. 保留 RC 阶段的真实平台、curl 与 OMP `session_stop` 验证证据，不把未执行的检查补写为通过。
5. 已完成的云端兼容验证必须准确表述为：卸载 v0.3.0 旧包后安装 `v1.0.0-rc.1`，同时保留原数据目录与配置数据。该结果支持卸载重装后的数据兼容性，不支持原位升级、在线更新或市场一键更新结论。
6. 确认 Release 配置将产生 `prerelease=false`、`make_latest=true`，且正式资产名为 `astrbot_plugin_webhook_notifier-v1.0.0.zip`。

### 发布后检查

1. 核对 GitHub Actions 成功，tag 指向预期提交，Release 非 draft、`prerelease=false` 且为 Latest。
2. 核对正式 ZIP 文件名、SHA256、单一插件根目录、版本三源和包内容契约。
3. 使用远端正式资产复核新装链路和核心 Webhook 行为。
4. 正式版发布并在 AstrBot 插件市场上架后，验证市场搜索安装、从已安装版本触发的一键更新/在线更新，以及更新后的数据与配置行为。
5. 市场更新验证完成前，文档和发布说明不得声称该路径已通过；若市场机制实际采用重装，也应按观察到的真实行为记录，不推断为原位升级。

市场更新验证属于发布后检查，不阻塞文档按 `v1.0.0` Final 定稿，但其结果必须在发布后单独留证。

---

## 手动兜底发布

如果 Actions 不可用，可以使用 GitHub CLI 手动发布：

```bash
uv sync --frozen --group dev
uv run --frozen pytest
npm ci --prefix frontend
npm run build --prefix frontend
uv run --frozen python scripts/package_plugin.py
gh release create v1.0.0 \
  dist/astrbot_plugin_webhook_notifier-v1.0.0.zip \
  --target main \
  --title "v1.0.0" \
  --notes-file tmp/release-v1.0.0-notes.md \
  --latest
```

手动兜底同样只能在发布前门槛全部满足后执行，并应再次确认创建结果不是 prerelease。当前尚未执行该命令。
