# 发布流程

本项目通过 GitHub Actions 自动构建插件 zip，并发布到 GitHub Release。当前 Workflow 使用 `setup-python`、pip 与 `python` 命令；本地推荐另用 uv 锁定环境验证，两者现阶段不是同一套依赖安装路径。

---

## 发布前检查

1. 确认版本号一致：
   - `metadata.yaml` 中的 `version` 使用 `vX.Y.Z`。
   - `pyproject.toml` 中的 `version` 使用 `X.Y.Z`。
   - `CHANGELOG.md` 顶部存在对应 `## vX.Y.Z - YYYY-MM-DD` 小节。
2. 维护本地锁定验证依赖：

   - `uv.lock` 必须随 `pyproject.toml` 的依赖声明一起维护并纳入提交。
   - 当前 `[dependency-groups].dev` 只包含 PyYAML、pytest、pytest-asyncio 与 Pillow，不包含 Ruff。
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
`v0.2.0-test.20260715.0905`。同一天多次打包时可直接按小时和分钟区分。

建议再添加本次测试用途标签，便于同时区分功能和生成时间：

```bash
uv run --frozen python scripts/package_plugin.py --dev-version --test-label template-manager
```

生成格式为 `-test.YYYYMMDD.HHMM.<label>`，例如
`v0.2.0-test.20260715.0905.template-manager`。标签仅允许英文字母、数字和连字符。

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

## 自动发布

推送版本 tag 会触发 `.github/workflows/release.yml`：

```bash
git tag v0.2.0
git push origin v0.2.0
```

当前 `.github/workflows/release.yml` 在创建 Release 前实际执行以下步骤：

- 使用 `actions/setup-python@v5` 配置 Python 3.13 和 pip cache，并使用 `actions/setup-node@v4` 配置 Node.js 20。
- 运行 `python -m pip install --upgrade pip`，再从 `requirements.txt` 并附加 `pyyaml pytest pytest-asyncio` 安装依赖。
- 使用 `npm ci --prefix frontend` 和 `npm run build --prefix frontend` 重建 Plugin Page。
- 校验 tag、`metadata.yaml` 与 `pyproject.toml` 版本一致。
- 运行 `python -m pytest`。
- 运行 `python scripts/package_plugin.py` 生成 `dist/*.zip`。
- 从 `CHANGELOG.md` 提取对应 tag 的发布说明。
- 创建或更新 GitHub Release，并上传插件 zip。

当前 CI 尚未使用 `uv.lock`，也没有 Ruff 门禁；将正式 CI 改为 uv 锁定安装属于后续改造项。在该改造完成前，不应把本地 uv 验证描述为现有 Release Workflow 已执行的步骤。若本地另行运行 Ruff，应视为独立可选检查，不属于当前 dev group 或发布 Workflow。

---

## 手动触发

如果 tag 已存在，也可以在 GitHub Actions 页面手动运行 `Release` workflow，并填写要发布的 tag，例如 `v0.2.0`。

---

## 手动兜底发布

如果 Actions 不可用，可以使用 GitHub CLI 手动发布：

```bash
uv sync --frozen --group dev
uv run --frozen pytest
npm ci --prefix frontend
npm run build --prefix frontend
uv run --frozen python scripts/package_plugin.py
gh release create v0.2.0 \
  dist/astrbot_plugin_webhook_notifier-v0.2.0.zip \
  --target main \
  --title "v0.2.0" \
  --notes-file tmp/release-v0.2.0-notes.md
```
