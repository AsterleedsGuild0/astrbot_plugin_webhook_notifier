from __future__ import annotations

import json
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


PLUGIN_NAME = "Webhook Notifier"


@register(
    "astrbot_plugin_webhook_notifier",
    "AsterleedsGuild0",
    "接收外部 Webhook 事件并推送到指定 AstrBot 会话",
    "v0.1.0",
)
class WebhookNotifierPlugin(Star):
    """AstrBot Webhook Notifier 插件骨架。

    当前版本只提供配置读取与状态命令，尚未启动 HTTP Webhook 服务。
    后续会在此基础上加入：Bearer Token 鉴权、事件标准化、HTML 卡片渲染、
    文本兜底和目标会话路由。
    """

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config

    async def initialize(self) -> None:
        if not self._enabled:
            logger.info("[WebhookNotifier] 插件已禁用，跳过初始化")
            return
        logger.info("[WebhookNotifier] 插件骨架已加载，Webhook 服务尚未实现")

    async def terminate(self) -> None:
        logger.info("[WebhookNotifier] 插件已卸载")

    @filter.command("webhook_notifier")
    async def webhook_notifier_status(self, event: AstrMessageEvent):
        """查看 Webhook Notifier 状态。"""
        yield event.plain_result(self._build_status_text())

    @filter.command("whn")
    async def webhook_notifier_short_status(self, event: AstrMessageEvent):
        """查看 Webhook Notifier 状态的短命令。"""
        yield event.plain_result(self._build_status_text())

    @property
    def _enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def _build_status_text(self) -> str:
        token_configured = bool(str(self.config.get("webhook_token", "")).strip())
        targets = self._config_text("targets")
        render_options = self._parse_render_options()

        lines = [
            f"{PLUGIN_NAME} 状态",
            "",
            f"启用状态：{'已启用' if self._enabled else '已禁用'}",
            "Webhook 服务：尚未实现（当前为初始化骨架）",
            f"默认渲染模式：{self.config.get('render_mode', 'text')}",
            f"渲染失败降级文本：{'开启' if self.config.get('fallback_to_text', True) else '关闭'}",
            f"Bearer Token：{'已配置' if token_configured else '未配置'}",
            f"模板目录：{self.config.get('templates_dir', 'templates')}",
            f"目标配置：{'已填写' if targets.strip() and not targets.lstrip().startswith('#') else '未填写'}",
            f"渲染参数：{self._compact_json(render_options)}",
            "",
            "规划：oh-my-pi/OMP session_stop → HTML 卡片/纯文本 → 指定 QQ 群聊或私聊。",
        ]
        return "\n".join(lines)

    def _config_text(self, key: str) -> str:
        value = self.config.get(key, "")
        return value if isinstance(value, str) else str(value)

    def _parse_render_options(self) -> dict[str, Any]:
        value = self.config.get("render_options", {})
        if isinstance(value, dict):
            return value
        if not isinstance(value, str) or not value.strip():
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            logger.warning(f"[WebhookNotifier] render_options JSON 解析失败：{e}")
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _compact_json(value: dict[str, Any]) -> str:
        if not value:
            return "{}"
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
