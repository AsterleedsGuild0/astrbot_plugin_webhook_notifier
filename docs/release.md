# 发布流程

本项目通过 GitHub Actions 自动构建插件 zip，并发布到 GitHub Release。

---

## 发布前检查

1. 确认版本号一致：
   - `metadata.yaml` 中的 `version` 使用 `vX.Y.Z`。
   - `pyproject.toml` 中的 `version` 使用 `X.Y.Z`。
   - `CHANGELOG.md` 顶部存在对应 `## vX.Y.Z - YYYY-MM-DD` 小节。
2. 本地运行测试：

```bash
python -m pytest
```

1. 本地生成带时分标识的测试包：

```bash
python scripts/package_plugin.py --dev-version
```

测试包版本和文件名使用本地时间后缀 `-test.YYYYMMDD.HHMM`，例如
`v0.2.0-test.20260715.0905`。同一天多次打包时可直接按小时和分钟区分。

建议再添加本次测试用途标签，便于同时区分功能和生成时间：

```bash
python scripts/package_plugin.py --dev-version --test-label template-manager
```

生成格式为 `-test.YYYYMMDD.HHMM.<label>`，例如
`v0.2.0-test.20260715.0905.template-manager`。标签仅允许英文字母、数字和连字符。

1. 本地验证正式发布包：

```bash
python scripts/package_plugin.py
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
git tag v0.1.0
git push origin v0.1.0
```

Workflow 会执行以下步骤：

- 安装运行和测试依赖。
- 校验 tag、`metadata.yaml` 与 `pyproject.toml` 版本一致。
- 运行 `python -m pytest`。
- 运行 `python scripts/package_plugin.py` 生成 `dist/*.zip`。
- 从 `CHANGELOG.md` 提取对应 tag 的发布说明。
- 创建或更新 GitHub Release，并上传插件 zip。

---

## 手动触发

如果 tag 已存在，也可以在 GitHub Actions 页面手动运行 `Release` workflow，并填写要发布的 tag，例如 `v0.1.0`。

---

## 手动兜底发布

如果 Actions 不可用，可以使用 GitHub CLI 手动发布：

```bash
python -m pytest
python scripts/package_plugin.py
gh release create v0.1.0 \
  dist/astrbot_plugin_webhook_notifier-v0.1.0.zip \
  --target main \
  --title "v0.1.0" \
  --notes-file tmp/release-v0.1.0-notes.md
```
