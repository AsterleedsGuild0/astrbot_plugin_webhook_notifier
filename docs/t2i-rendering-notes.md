# T2I HTML 卡片渲染经验

本文记录 `astrbot_plugin_webhook_notifier` 在 HTML 卡片截图阶段的可复用经验，重点覆盖 Playwright / AstrBot `html_render` / T2I 截图时出现的多余视口背景裁剪问题。

---

## 右侧或底部大块空白的根因

AstrBot `html_render` 最终会把 HTML 交给 T2I 服务截图。`full_page=true` 能让高度随内容扩展，但截图宽度仍可能保留浏览器视口宽度。

如果 HTML 模板只把卡片放在视口左上角，而 `html` / `body` 没有收缩到内容宽度，最终图片右侧或底部就会保留大块页面背景。

---

## 推荐处理顺序

优先按以下顺序处理，不要一开始就依赖静态 `clip`：

1. **CSS shrinkwrap**：让 `html` 和 `body` 使用 `width: fit-content`、`min-height: 0`、`height: auto`，避免页面容器强制铺满默认视口。
2. **显式 viewport 参数**：向 `html_render` 传入 `viewport_width`、`viewport_height`、`full_page`、`device_scale_factor_level` 等参数，减少不同 T2I 服务默认值差异。
3. **本地图片后处理裁剪**：当 `html_render(return_url=False)` 返回本地图片路径时，对图片文件做右侧和底部空白裁剪；URL、base64、bytes 等结果保持原样。
4. **像素检测只做兜底**：已知模板宽度时优先按设计画布宽度推断右边界；底部高度动态时再用像素差异检测寻找内容边缘。

静态 `clip` 不适合作为默认方案，因为卡片高度会随字段数量、摘要、长路径等内容变化，容易裁掉底部信息。

---

## 当前插件采用的策略

默认模板采用固定卡片内容宽度加收缩页面容器：

```css
html,
body {
  width: fit-content;
  min-width: 0;
  min-height: 0;
  height: auto;
}

.card {
  width: 828px;
  max-width: 828px;
}
```

默认截图参数：

```json
{
  "full_page": true,
  "type": "png",
  "quality": 90,
  "timeout": 5000,
  "viewport_width": 860,
  "viewport_height": 1200,
  "device_scale_factor_level": "high",
  "wait_until": "domcontentloaded"
}
```

渲染时使用 `return_url=false` 获取本地图片路径，然后执行 `trim_viewport_whitespace()`：

- 仅处理本地文件路径。
- 按已知画布宽度裁掉右侧多余视口背景。
- 兼容 T2I 尊重 `viewport_width=860` 和旧服务退回默认 `1280` 视口两类情况。
- 用像素差异检测裁掉底部背景。
- 先写临时文件，再校验并原子替换原图；失败时跳过裁剪，不影响文本降级和发送链路。

---

## 参考来源

本方案借鉴了 `astrbot_plugin_bilibili` 的近期裁剪实现经验：

- 模板侧使用 `width: fit-content`、`min-height: 0` 让页面容器贴合卡片。
- 渲染侧使用 `return_url=false` 获取本地图片路径。
- 图片侧使用已知模板宽度与像素差异检测组合裁掉右侧/底部多余背景。
- 裁剪失败时静默跳过，不让图片后处理破坏主通知链路。

适配到本插件时做了简化：当前只有一个默认 HTML 模板，因此不需要多模板宽度映射；只保留固定画布宽度推断和底部像素检测。
