# Monaco Plugin Page Phase 0 验证

## Claim 与证据路径

本阶段仅验证插件自行打包的 Monaco Editor、HTML language worker 与 AstrBot Plugin Page 静态资源机制可以组合运行。前端源码位于 `frontend/`，可发布验证页位于 `pages/template-editor/`，自动检查位于 `tests/test_frontend_build.py`。

---

## 为什么不复用 Dashboard Monaco

Plugin Page 运行在受限 iframe 中，不应依赖 Dashboard 的内部模块路径、构建产物或运行时依赖。Dashboard 升级也可能改变这些非公开接口，因此验证页独立固定 `monaco-editor@0.52.2`。

---

## 为什么使用 inline worker

Plugin Page 资源 URL 可能附带 `asset_token`，独立 worker chunk 的二次加载可能受到 token 传播、iframe 或资源访问策略影响。Vite 的 `?worker&inline` 将 editor、HTML、CSS、JSON worker 包装进主 JavaScript 产物，避免发布额外 worker 文件。

---

## 静态验证

```bash
npm ci --prefix frontend
npm run build --prefix frontend
python -m pytest tests/test_frontend_build.py
```

页面会显示并暴露 `window.__WHN_MONACO_PHASE0__`，其中记录编辑器加载、worker label 请求、worker message/error、HTML/CSS/JSON markers、资源请求和外部资源状态。验证代码在主 HTML editor 创建后，通过 Monaco 公共 editor action `editor.action.formatDocument` 强制调用 HTML language worker；由于 HTML language mode 采用动态 import 异步初始化，验证代码会先等待 action 报告 `isSupported()`，然后只执行一次格式化并等待实际 HTML worker request。HTML worker 的真实 `message` 事件作为成功证据；CSS/JSON 继续通过包含故意语法错误的隐藏 models 触发 language workers并记录 markers。`workersVerified.html/css/json` 只会在对应 worker 真实返回 message 后变为 `true`，验证完成后隐藏 models 会被释放。全局 `error` 与 `unhandledrejection` 也会写入验证状态，避免初始化异常时页面永久停留在 `loading`。也可用静态服务器打开 `pages/template-editor/index.html`；没有 `window.AstrBotPluginPage` bridge 时页面应继续运行。

---

## 验证边界

静态构建和自动化测试只能证明产物结构、自包含资源引用以及 diagnostics 验证逻辑存在。测试不会把 Monaco bundle 内的 URL 字面量当作真实网络请求证据，也不会为通过测试而替换或清洗 bundle 内容。真实网络证据来自浏览器 `PerformanceObserver` 记录并汇总到 `externalResources` 的实际资源请求。

HTML 的证据是格式化动作触发后收到真实 worker message，HTML diagnostics 可以为 0；CSS/JSON 则同时保留故意错误 models、markers 计数与真实 worker message 证据。页面仅在 HTML/CSS/JSON worker 都返回 message 后进入 `ready`，`workerRequests` 只作为辅助信息。

2026-07-15 已使用 AstrBot `PluginPageService` 真实 URL 重写和安全头逻辑构造 token-gated harness，并在 `sandbox="allow-scripts allow-forms allow-downloads"` 的无同源 iframe 中完成浏览器验证：页面状态为 `ready`，HTML/CSS/JSON worker 分别返回消息，`workerErrors=[]`，所有 HTML、CSS、JavaScript 与 bridge 请求均携带有效 `asset_token` 并返回 `200`，`externalResources=[]`。worker 以 `blob:null/...` 形式运行，证明当前 inline worker 方案无需额外 worker 静态资源请求。该结果覆盖 Plugin Page 的关键静态资源与 sandbox 约束，但不代替完整 AstrBot Dashboard 实机回归。

---

## Phase 1 交付与验证

Phase 1 已将验证页升级为可用的模板管理页面。管理员从 AstrBot 插件详情页进入页面后，可完成模板列表浏览、内置模板只读查看、新建、复制、删除、保存、应用、保存并应用，以及未保存内容离开确认。编辑区使用 HTML Monaco，预览数据使用 JSON Monaco；服务端返回的安全 HTML 通过无权限 `sandbox` iframe 的 `srcdoc` 展示。

页面只通过 `window.AstrBotPluginPage` bridge 调用插件注册的相对 endpoint，不拼接 Dashboard 地址，也不要求管理员直接访问内部 API。bridge 覆盖模板列表、详情、保存、应用、删除和 preview；`expected_revision` 冲突会以 409 提示重新载入。preview 仅渲染当前会话提交的模板与 JSON 数据，不写入 registry。

页面还通过认证 bridge 调用 `GET /astrbot_plugin_webhook_notifier/base-url`。响应严格只包含 `base_url` 与 `configured`，不返回 Token、Registry、endpoint 列表、owner、UMO 或 server secret。Plugin Page 展示并复制 Base URL；用户在页面外将其与聊天中获得的 Endpoint Path 组合，页面不管理或缓存 Token。

交付产物固定使用 `monaco-editor@0.52.2`、Vite 6.4.3 和 4 个 inline workers。`asset_token`、无同源 sandbox、worker 消息和无外部资源验证继续沿用 Phase 0 证据；具体产物大小以当前 production build 和 `tests/test_frontend_build.py` 的限制检查为准，不在文档中固化易过期的包大小。
