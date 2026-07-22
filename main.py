from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import traceback
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Awaitable, cast
from urllib.parse import urlsplit

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.platform.message_type import MessageType

from .core.help_card import (
    HELP_CARD_BODY_PADDING,
    HELP_CARD_CANVAS_WIDTH,
    HELP_CARD_RENDER_OPTIONS,
    HELP_CARD_WIDTH,
    build_help_text,
    render_help_card_html,
)
from .core.models import EndpointStatus, ServerConfig
from .core.omp import OmpProviderAdapter
from .core.opencode import OpenCodeProviderAdapter
from .core.providers import ProviderRegistry
from .core.registry import (
    BIND_CURRENT_GROUP,
    PREBOUND_GROUP,
    EndpointRegistry,
    normalize_endpoint_name,
)
from .core.renderer import (
    render_preview,
    trim_viewport_whitespace,
    validate_image_result,
)
from .core.sender import Sender
from .core.security import TOKEN_PREFIX
from .core.server import WebhookServer
from .core.template_registry import (
    MAX_TEMPLATE_BYTES,
    MAX_TEMPLATES,
    TemplateConflictError,
    TemplateReadOnlyError,
    TemplateRegistry,
    TemplateRegistryError,
)

PLUGIN_NAME = "Webhook Notifier"
COMMAND = "whn"
WAKE_PREFIX_PLACEHOLDER = "<AstrBot唤醒词>"
WAKE_PREFIX_DIAGNOSTIC = "无法读取当前会话唤醒词，请检查 AstrBot 配置和插件日志"


@dataclass(frozen=True)
class _CommandRoots:
    short: str
    long: str
    config_error: bool = False


PLACEHOLDER_COMMAND_ROOTS = _CommandRoots(
    short=f"{WAKE_PREFIX_PLACEHOLDER}whn",
    long=f"{WAKE_PREFIX_PLACEHOLDER}webhook_notifier",
    config_error=True,
)


class _SafeStatusText(str):
    """由受控字段构造、可绕过通用 URL/domain 清洗的状态文本。"""


class _TokenDeliveryText(str):
    """仅用于 direct send 的敏感 Token 文本，禁止转为 yield result。"""

    endpoint_name: str

    def __new__(cls, value: str, endpoint_name: str):
        instance = super().__new__(cls, value)
        instance.endpoint_name = endpoint_name
        return instance


class _ExactSecretFilter(logging.Filter):
    """仅清洗当前 direct-send Token 精确值。"""

    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        if self.secret in rendered:
            record.msg = rendered.replace(self.secret, "[REDACTED]")
            record.args = ()
        for key, value in tuple(record.__dict__.items()):
            if isinstance(value, str) and self.secret in value:
                record.__dict__[key] = value.replace(self.secret, "[REDACTED]")
        if record.exc_info:
            try:
                formatted_exc = "".join(traceback.format_exception(*record.exc_info))
            except Exception:
                formatted_exc = ""
            if self.secret in formatted_exc:
                record.exc_info = None
                record.exc_text = "[REDACTED]"
        if isinstance(record.exc_text, str) and self.secret in record.exc_text:
            record.exc_text = record.exc_text.replace(self.secret, "[REDACTED]")
        if isinstance(record.stack_info, str) and self.secret in record.stack_info:
            record.stack_info = record.stack_info.replace(self.secret, "[REDACTED]")
        return True


TOKEN_PLAINTEXT_PATTERN = re.compile(
    rf"\b{re.escape(TOKEN_PREFIX)}[A-Za-z0-9_-]{{43}}\b"
)


def _command_positional_args_compat(handler: Any) -> Any:
    """Allow legacy positional command args without exposing varargs to filters.

    AstrBot versions that inspect command handler signatures should see only
    ``self`` and ``event``. Older runtimes may still pass parsed command words
    positionally, which the underlying function accepts via ``*args``.
    """
    signature = inspect.signature(handler)
    visible_parameters = list(signature.parameters.values())[:2]
    setattr(
        handler,
        "__signature__",
        signature.replace(parameters=visible_parameters),
    )
    return handler


@register(
    "astrbot_plugin_webhook_notifier",
    "AsterleedsGuild0",
    "接收外部 Webhook 事件并推送到指定 AstrBot 会话",
    "v1.0.0",
)
class WebhookNotifierPlugin(Star):
    """接收 Webhook 事件并安全投递到 AstrBot 会话。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config

        # 运行时组件（在 initialize 中初始化）
        self._registry: EndpointRegistry | None = None
        self._template_registry: TemplateRegistry | None = None
        self._sender: Sender | None = None
        self._server: WebhookServer | None = None
        self._server_config: ServerConfig | None = None
        # 默认 ProviderRegistry 包含 OMP adapter，确保构造后即可用（测试无需 initialize）
        self._provider_registry: ProviderRegistry | None = None
        self._init_default_provider_registry()
        self._register_template_web_apis()

    async def initialize(self) -> None:
        if not self._enabled:
            logger.info("[WebhookNotifier] 插件已禁用，跳过初始化")
            return

        # 初始化插件数据目录
        data_dir_available = True
        try:
            data_dir = StarTools.get_data_dir(
                plugin_name="astrbot_plugin_webhook_notifier"
            )
        except (ValueError, RuntimeError) as e:
            data_dir_available = False
            logger.error(
                f"[WebhookNotifier] 无法获取插件数据目录: {e}，将使用 fallback 路径"
            )
            data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(data_dir, exist_ok=True)
        logger.info(f"[WebhookNotifier] 插件数据目录: {data_dir}")

        # 验证 ProviderRegistry 已初始化（构造器已创建默认 OMP Registry 并冻结）
        if self._provider_registry is None:
            logger.error("[WebhookNotifier] ProviderRegistry 未初始化，重新初始化")
            self._init_default_provider_registry()

        self._registry = EndpointRegistry(data_dir)
        self._registry.expire_stale_pending()
        if data_dir_available:
            try:
                self._template_registry = TemplateRegistry(data_dir)
            except Exception as e:
                logger.error(f"[WebhookNotifier] 模板 Registry 初始化失败: {e}")
        else:
            logger.error(
                "[WebhookNotifier] StarTools 数据目录不可用，模板 Registry 已禁用"
            )

        # 初始化 Sender
        self._sender = Sender(
            self.context,
            bool(self.config.get("enable_private_notifications", False)),
        )

        # 初始化服务器配置
        self._server_config = ServerConfig.from_plugin_config(
            self.config  # type: ignore[arg-type]
        )

        # 如果有任何 active endpoint，启动 HTTP 服务
        if self._registry.count_deliverable() > 0:
            await self._start_server()
        else:
            logger.info(
                "[WebhookNotifier] 暂无 active endpoint，HTTP 服务未启动。"
                "创建 endpoint 后服务将自动启动。"
            )

        # 检查早期占位 webhook_token 配置。该键不在 MVP 配置界面展示；
        # 仅当用户从骨架阶段配置升级且本地仍保留该键时提示。
        legacy_token = str(self.config.get("webhook_token", "")).strip()
        if legacy_token:
            logger.warning(
                "[WebhookNotifier] 检测到早期占位 webhook_token 配置，该配置在 MVP 中未启用。"
                "请使用 <AstrBot唤醒词>whn token new 命令创建 managed endpoint。"
            )

        logger.info("[WebhookNotifier] 插件已初始化")

    async def terminate(self) -> None:
        if self._server:
            await self._server.stop()
        logger.info("[WebhookNotifier] 插件已卸载")

    # ─── Template Web API ───────────────────────────────────

    def _register_template_web_apis(self) -> None:
        """在构造期注册 Plugin Page API。"""
        prefix = "/astrbot_plugin_webhook_notifier"
        routes = (
            ("base-url", self._api_base_url, ["GET"], "Get Webhook Base URL"),
            ("templates", self._api_templates, ["GET"], "List templates"),
            (
                "templates/<template_id>",
                self._api_template_detail,
                ["GET"],
                "Get template",
            ),
            ("templates/save", self._api_template_save, ["POST"], "Save template"),
            (
                "templates/apply",
                self._api_template_apply,
                ["POST"],
                "Apply template",
            ),
            (
                "templates/delete",
                self._api_template_delete,
                ["POST"],
                "Delete template",
            ),
            (
                "templates/preview",
                self._api_template_preview,
                ["POST"],
                "Preview template",
            ),
        )
        for endpoint, handler, methods, description in routes:
            self.context.register_web_api(
                f"{prefix}/{endpoint}", handler, methods, description
            )

    async def _api_base_url(self):
        """返回 Plugin Page 展示 Webhook Base URL 所需的最小只读数据。"""
        config = self._server_config or ServerConfig.from_plugin_config(
            self.config  # type: ignore[arg-type]
        )
        configured_base = str(config.public_base_url).strip()
        if configured_base:
            return json_response(
                {"base_url": configured_base.rstrip("/"), "configured": True}
            )
        base_path = config.base_path.rstrip("/")
        return json_response(
            {
                "base_url": f"http://{config.host}:{config.port}{base_path}",
                "configured": False,
            }
        )

    def _template_registry_or_response(self):
        if self._template_registry is None:
            return None, error_response("模板 Registry 未初始化", 503)
        return self._template_registry, None

    async def _api_templates(self):
        registry, unavailable = self._template_registry_or_response()
        if unavailable:
            return unavailable
        assert registry is not None
        snapshot = registry.snapshot
        return json_response(
            {
                "templates": registry.list_templates(),
                "active": snapshot.active,
                "requested_active": snapshot.active,
                "effective_active": snapshot.effective_active,
                "read_only": snapshot.read_only,
                "sample_event": self._sample_preview_event(),
                "limits": {
                    "max_templates": MAX_TEMPLATES,
                    "max_template_bytes": MAX_TEMPLATE_BYTES,
                    "canvas_width_min": 320,
                    "canvas_width_max": 2048,
                },
            }
        )

    async def _api_template_detail(self, template_id: str | None = None):
        registry, unavailable = self._template_registry_or_response()
        if unavailable:
            return unavailable
        assert registry is not None
        template_id = template_id or self._request_template_id()
        result = registry.export_template(template_id)
        if result is None:
            return error_response("模板不存在", 404)
        return json_response(result)

    async def _api_template_save(self):
        registry, unavailable = self._template_registry_or_response()
        if unavailable:
            return unavailable
        assert registry is not None
        try:
            body = await self._request_json()
            apply_value = body.get("apply", False)
            if not isinstance(apply_value, bool):
                raise ValueError("apply 必须是 bool")
            result = registry.save(
                body.get("id"),
                body.get("display_name"),
                body.get("content"),
                body.get("canvas_width"),
                body.get("expected_revision"),
                apply_value,
            )
            snapshot = registry.snapshot
            return json_response(
                {
                    "template": registry.export_template(result.id),
                    "active": snapshot.active,
                    "effective_active": snapshot.effective_active,
                }
            )
        except TemplateConflictError as e:
            return error_response(str(e), 409)
        except TemplateReadOnlyError as e:
            return error_response(str(e), 503)
        except (TemplateRegistryError, ValueError, TypeError) as e:
            return error_response(str(e), 400)

    async def _api_template_apply(self):
        registry, unavailable = self._template_registry_or_response()
        if unavailable:
            return unavailable
        assert registry is not None
        try:
            body = await self._request_json()
            registry.apply(body.get("id"), body.get("expected_revision"))
            snapshot = registry.snapshot
            return json_response(
                {
                    "active": snapshot.active,
                    "effective_active": snapshot.effective_active,
                }
            )
        except TemplateConflictError as e:
            return error_response(str(e), 409)
        except TemplateReadOnlyError as e:
            return error_response(str(e), 503)
        except (TemplateRegistryError, ValueError, TypeError) as e:
            return error_response(str(e), 400)

    async def _api_template_delete(self):
        registry, unavailable = self._template_registry_or_response()
        if unavailable:
            return unavailable
        assert registry is not None
        try:
            body = await self._request_json()
            registry.delete(body.get("id"), body.get("expected_revision"))
            snapshot = registry.snapshot
            return json_response(
                {
                    "deleted": True,
                    "active": snapshot.active,
                    "effective_active": snapshot.effective_active,
                }
            )
        except TemplateConflictError as e:
            return error_response(str(e), 409)
        except TemplateReadOnlyError as e:
            return error_response(str(e), 503)
        except (TemplateRegistryError, ValueError, TypeError) as e:
            return error_response(str(e), 400)

    async def _api_template_preview(self):
        registry, unavailable = self._template_registry_or_response()
        if unavailable:
            return unavailable
        assert registry is not None
        try:
            body = await self._request_json()
            content = body.get("content")
            width = body.get("canvas_width")
            template_id = body.get("id")
            if content is None and template_id:
                template = registry.get(str(template_id))
                if template is None or not template.valid:
                    raise TemplateRegistryError("模板不存在或无效")
                content = template.content
                width = template.canvas_width if width is None else width
            if not isinstance(content, str):
                raise ValueError("content 必须是字符串")
            html, width = render_preview(content, body.get("event"), width)
            return json_response({"html": html, "canvas_width": width})
        except (TemplateRegistryError, ValueError, TypeError) as e:
            return error_response(str(e), 400)

    @staticmethod
    async def _request_json() -> dict[str, Any]:
        value = request.json
        if callable(value):
            value = value()
        if inspect.isawaitable(value):
            value = await cast(Awaitable[Any], value)
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON object")
        return value

    @staticmethod
    def _request_template_id() -> str:
        view_args = getattr(request, "view_args", None) or {}
        return str(view_args.get("template_id", ""))

    @staticmethod
    def _sample_preview_event() -> dict[str, Any]:
        return {
            "title": "Webhook 通知",
            "source": "AstrBot",
            "status": "success",
            "summary": "模板预览示例",
            "fields": [{"label": "事件", "value": "preview"}],
        }

    # ─── 状态命令 ───────────────────────────────────────────

    @filter.command("webhook_notifier")
    async def status_long(self, event: AstrMessageEvent):
        """查看 Webhook Notifier 完整状态。"""
        commands = self._resolve_command_roots(event)
        yield self._plain_text_result(event, self._build_status_text(commands))

    @filter.command("whn")
    @_command_positional_args_compat
    async def status_short(self, event: AstrMessageEvent, *command_args: object):
        """Webhook Notifier 短命令入口。"""
        commands = self._resolve_command_roots(event)
        if command_args:
            injected_text = " ".join(str(arg) for arg in command_args).strip()
            args = self._normalize_whn_args(injected_text)
        else:
            args = self._normalize_whn_args(event.message_str)
        if not args:
            yield self._plain_text_result(event, self._build_status_text(commands))
            return

        result = await self._dispatch_whn_command(event, args, commands)
        if isinstance(result, _SafeStatusText):
            yield self._plain_text_result(event, result)
        elif isinstance(result, _TokenDeliveryText):
            sent = await self._send_sensitive_plain(event, result)
            if not sent:
                yield self._credential_delivery_failure_result(
                    event, result.endpoint_name, commands
                )
        elif isinstance(result, str):
            yield self._plain_text_result(event, self._sanitize_chat_text(result))
        elif isinstance(result, list):
            for item in result:
                if isinstance(item, _TokenDeliveryText):
                    sent = await self._send_sensitive_plain(event, item)
                    if not sent:
                        yield self._credential_delivery_failure_result(
                            event, item.endpoint_name, commands
                        )
                elif isinstance(item, str):
                    yield self._plain_text_result(event, self._sanitize_chat_text(item))
                else:
                    yield item
        elif result is not None:
            yield result

    def _plain_text_result(self, event: AstrMessageEvent, text: str):
        """构造强制纯文本的命令响应，避免 URL/Token 被 AstrBot T2I 转成图片。"""
        return event.plain_result(text).use_t2i(False)

    def _credential_delivery_failure_result(
        self,
        event: AstrMessageEvent,
        endpoint_name: str,
        commands: _CommandRoots = PLACEHOLDER_COMMAND_ROOTS,
    ):
        return self._plain_text_result(
            event,
            "⚠️ 凭据发送未确认，endpoint 状态已保存。请在同一 Bot 私聊执行 "
            f"{commands.short} token rotate {endpoint_name} 生成并发送新 Token。",
        )

    async def _send_sensitive_plain(
        self, event: AstrMessageEvent, delivery: _TokenDeliveryText
    ) -> bool:
        """绕过 RespondStage 单次发送敏感 Plain，并精确清洗当前 Token 日志。"""
        text = str(delivery)
        prefix = "Bearer Token: "
        if not text.startswith(prefix):
            raise ValueError("敏感凭据消息格式非法")
        secret = text.removeprefix(prefix)
        chain = MessageChain([Plain(text)]).use_t2i(False).use_markdown(False)
        secret_filter = _ExactSecretFilter(secret)
        targets = self._install_sensitive_log_filter(secret_filter)
        try:
            await event.send(chain)
            return True
        except Exception as exc:
            logger.error(
                "[WebhookNotifier] 敏感凭据 direct send 失败 "
                f"error={type(exc).__name__}"
            )
            return False
        finally:
            self._remove_sensitive_log_filter(secret_filter, targets)

    @staticmethod
    def _install_sensitive_log_filter(
        secret_filter: logging.Filter,
    ) -> list[logging.Filterer]:
        targets: list[logging.Filterer] = []
        seen: set[int] = set()

        def attach(target: logging.Filterer) -> None:
            identity = id(target)
            if identity in seen:
                return
            seen.add(identity)
            target.addFilter(secret_filter)
            targets.append(target)

        root = logging.getLogger()
        attach(root)
        for handler in root.handlers:
            attach(handler)

        prefixes = ("astrbot", "botpy", "aiocqhttp")
        for name in prefixes:
            attach(logging.getLogger(name))
        for name, candidate in logging.Logger.manager.loggerDict.items():
            if not isinstance(candidate, logging.Logger):
                continue
            if not any(
                name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes
            ):
                continue
            attach(candidate)
            for handler in candidate.handlers:
                attach(handler)
        return targets

    @staticmethod
    def _remove_sensitive_log_filter(
        secret_filter: logging.Filter, targets: list[logging.Filterer]
    ) -> None:
        for target in reversed(targets):
            target.removeFilter(secret_filter)

    def _sanitize_chat_text(self, text: str) -> str:
        """对所有文本命令回复执行 URL 与配置值的最终防泄漏处理。"""
        sanitized = str(text)
        server_config = self._server_config or ServerConfig.from_plugin_config(
            self.config  # type: ignore[arg-type]
        )
        configured = str(server_config.public_base_url).strip()
        if configured:
            sanitized = sanitized.replace(configured, "[已隐藏]")
            parsed = urlsplit(configured)
            for sensitive_value in (parsed.netloc, parsed.hostname):
                if sensitive_value:
                    sanitized = sanitized.replace(sensitive_value, "[已隐藏]")
        sanitized = re.sub(r"https?://\S+", "[已隐藏]", sanitized, flags=re.I)
        sanitized = TOKEN_PLAINTEXT_PATTERN.sub("[Token 已隐藏]", sanitized)
        sanitized = re.sub(
            r"(?m)^.*OMP_SESSION_WEBHOOK_URL\s*=.*(?:\n|$)", "", sanitized
        )
        return sanitized.rstrip()

    def _resolve_command_roots(self, event: AstrMessageEvent) -> _CommandRoots:
        """同步读取并严格校验当前会话的 AstrBot 唤醒词。"""
        try:
            config = self.context.get_config(event.unified_msg_origin)
        except Exception as exc:
            self._log_wake_prefix_error("get_config_exception", type(exc).__name__)
            return PLACEHOLDER_COMMAND_ROOTS

        if not isinstance(config, Mapping):
            self._log_wake_prefix_error("config_container_type", type(config).__name__)
            return PLACEHOLDER_COMMAND_ROOTS
        if "wake_prefix" not in config:
            self._log_wake_prefix_error("wake_prefix_missing", "missing_key")
            return PLACEHOLDER_COMMAND_ROOTS

        wake_prefix = config["wake_prefix"]
        if not isinstance(wake_prefix, list):
            self._log_wake_prefix_error(
                "wake_prefix_container_type", type(wake_prefix).__name__
            )
            return PLACEHOLDER_COMMAND_ROOTS
        for prefix in wake_prefix:
            if not isinstance(prefix, str):
                self._log_wake_prefix_error(
                    "wake_prefix_element_type", type(prefix).__name__
                )
                return PLACEHOLDER_COMMAND_ROOTS
            if any(unicodedata.category(character) == "Cc" for character in prefix):
                self._log_wake_prefix_error(
                    "wake_prefix_control_character", "unicode_control_character"
                )
                return PLACEHOLDER_COMMAND_ROOTS

        prefix = wake_prefix[0] if wake_prefix else ""
        return _CommandRoots(short=f"{prefix}whn", long=f"{prefix}webhook_notifier")

    @staticmethod
    def _log_wake_prefix_error(category: str, reason: str) -> None:
        logger.error(
            f"[WebhookNotifier] 当前会话 wake_prefix 配置异常 "
            f"category={category} reason={reason}"
        )

    def _normalize_whn_args(self, message: str) -> str:
        """规范化 /whn 命令参数。

        AstrBot v4.26.1 的 `event.message_str` 可能仍包含完整命令文本
        （例如 `whn status`），而不是只包含命令后的参数。这里兼容两种
        形态，避免把根命令 `whn` 当作子命令处理。
        """
        text = (message or "").strip()
        if not text:
            return ""

        if text.startswith("/"):
            text = text[1:].lstrip()

        parts = text.split(maxsplit=1)
        if parts and parts[0].lower() == COMMAND:
            return parts[1].strip() if len(parts) > 1 else ""
        return text

    async def _dispatch_whn_command(
        self,
        event: AstrMessageEvent,
        args: str,
        commands: _CommandRoots | None = None,
    ) -> Any:
        """分发 /whn 子命令。"""
        if commands is None:
            commands = self._resolve_command_roots(event)
        parts = args.split()
        if not parts:
            return self._build_status_text(commands)

        sub = parts[0].lower()

        if sub in ("help", "帮助"):
            return await self._build_help_result(event, commands)
        if sub in ("status", "状态"):
            return self._build_status_text(commands)
        if sub == "token":
            return await self._handle_token_command(event, parts[1:], commands)
        if sub == "admin":
            return self._handle_admin_command(event, parts[1:], commands)
        return f"❌ 未知子命令：{sub}\n发送 {commands.short} help 查看可用命令。"

    async def _build_help_result(
        self, event: AstrMessageEvent, commands: _CommandRoots
    ):
        """优先返回内置帮助图片，任何渲染异常都降级为纯文本。"""
        is_admin = self._event_is_super_admin(event)
        try:
            rendered_html = render_help_card_html(
                is_admin, commands.short, commands.config_error
            )
            image_result = await self.html_render(
                "{{ rendered_html | safe }}",
                {"rendered_html": rendered_html},
                return_url=False,
                options=dict(HELP_CARD_RENDER_OPTIONS),
            )
            validate_image_result(image_result)
            image_result = trim_viewport_whitespace(
                image_result,
                canvas_width=HELP_CARD_CANVAS_WIDTH,
                card_width=HELP_CARD_WIDTH,
                body_padding=HELP_CARD_BODY_PADDING,
            )
            return event.chain_result([Image(file=image_result)]).use_t2i(False)
        except Exception as exc:
            logger.warning(f"[WebhookNotifier] 帮助卡片渲染失败，回退纯文本: {exc}")
            return self._plain_text_result(
                event, build_help_text(is_admin, commands.short, commands.config_error)
            )

    @staticmethod
    def _event_is_super_admin(event: AstrMessageEvent) -> bool:
        try:
            return event.is_admin() is True
        except (AttributeError, TypeError, NotImplementedError):
            return False

    def _handle_admin_command(
        self,
        event: AstrMessageEvent,
        args: list[str],
        commands: _CommandRoots = PLACEHOLDER_COMMAND_ROOTS,
    ) -> str:
        """处理仅 AstrBot 全局超级管理员可用的 Registry 命令。"""
        sender_id = event.get_sender_id()
        action = " ".join(args[:2]) or "usage"
        if not event.is_admin():
            logger.warning(
                f"[WebhookNotifier] Registry 管理操作拒绝 sender={sender_id} "
                f"action={action} result=permission_denied"
            )
            return "❌ 权限不足：此命令仅 AstrBot 全局超级管理员可用。"

        if not self._is_private(event):
            logger.warning(
                f"[WebhookNotifier] Registry 管理操作拒绝 sender={sender_id} "
                f"action={action} result=private_only"
            )
            return "❌ Registry 管理命令请在私聊中执行。"

        if not args:
            logger.warning(
                f"[WebhookNotifier] Registry 管理操作 sender={sender_id} "
                "action=usage result=invalid_input"
            )
            return self._admin_command_usage(commands.short)
        if args[0].lower() != "token":
            logger.warning(
                f"[WebhookNotifier] Registry 管理操作 sender={sender_id} "
                f"action={args[0].lower()} result=unknown_action"
            )
            return (
                f"❌ 未知 admin 子命令：{args[0].lower()}\n"
                f"发送 {commands.short} help 查看可用命令。"
            )
        if len(args) < 2:
            logger.warning(
                f"[WebhookNotifier] Registry 管理操作 sender={sender_id} "
                "action=token result=invalid_input"
            )
            return self._admin_command_usage(commands.short)

        token_action = args[1].lower()
        if token_action == "list":
            if len(args) != 2:
                logger.warning(
                    f"[WebhookNotifier] Registry 管理操作 sender={sender_id} "
                    "action=list result=invalid_input"
                )
                return f"❌ 用法: {commands.short} admin token list"
            return self._handle_admin_token_list(sender_id)
        if token_action == "revoke-path":
            return self._handle_admin_token_revoke_path(
                sender_id, args[2:], commands.short
            )
        if token_action == "revoke-owner":
            return self._handle_admin_token_revoke_owner(
                sender_id, args[2:], commands.short
            )
        logger.warning(
            f"[WebhookNotifier] Registry 管理操作 sender={sender_id} "
            f"action={token_action} result=unknown_action"
        )
        return (
            f"❌ 未知 admin token 子命令：{token_action}\n"
            f"发送 {commands.short} help 查看可用命令。"
        )

    @staticmethod
    def _admin_command_usage(command_root: str) -> str:
        return (
            "用法:\n"
            f"  {command_root} admin token list\n"
            f"  {command_root} admin token revoke-path <endpoint-path>\n"
            f"  {command_root} admin token revoke-owner <platform_id> <owner_user_id> <名称>\n"
            "仅 AstrBot 全局超级管理员可用，且必须在私聊执行。"
        )

    def _handle_admin_token_list(self, sender_id: str) -> str:
        registry = self._ensure_registry()
        if not registry:
            logger.error(
                f"[WebhookNotifier] Registry 管理操作 sender={sender_id} "
                "action=list result=registry_unavailable"
            )
            return "❌ Registry 未初始化，请检查日志。"

        records = registry.list_all_for_admin()
        logger.info(
            f"[WebhookNotifier] Registry 管理操作 sender={sender_id} "
            f"action=list result=success count={len(records)}"
        )
        if not records:
            return "📋 Registry 中暂无 endpoint。"

        limit = 50
        lines = [f"📋 全部 Endpoint（共 {len(records)} 条，最多显示 {limit} 条）:"]
        for index, rec in enumerate(records[:limit], start=1):
            target_names = ",".join(target.name for target in rec.targets) or "无"
            lines.append(
                f"{index}. owner={rec.owner_user_id} | name={rec.name} | "
                f"provider={rec.provider} | Endpoint Path={rec.path} | "
                f"status={rec.status} | "
                f"targets={target_names} ({len(rec.targets)}) | "
                f"created={rec.created_at[:19]}"
            )
        if len(records) > limit:
            lines.append(f"… 另有 {len(records) - limit} 条未显示。")
        lines.append("安全提示：此列表不会显示 Token 明文、hash 或完整目标 UMO。")
        return "\n".join(lines)

    def _handle_admin_token_revoke_path(
        self,
        sender_id: str,
        args: list[str],
        command_root: str = PLACEHOLDER_COMMAND_ROOTS.short,
    ) -> str:
        if len(args) != 1 or not args[0].strip():
            logger.warning(
                f"[WebhookNotifier] Registry 管理操作 sender={sender_id} "
                "action=revoke-path result=invalid_input"
            )
            return f"❌ 用法: {command_root} admin token revoke-path <endpoint-path>"

        path = args[0].strip()
        if path.startswith("/"):
            path = path[1:]
        if not path:
            logger.warning(
                f"[WebhookNotifier] Registry 管理操作 sender={sender_id} "
                "action=revoke-path result=empty_path"
            )
            return "❌ endpoint path 不能为空。"
        path_id = sha256(path.encode("utf-8")).hexdigest()[:12]

        registry = self._ensure_registry()
        if not registry:
            logger.error(
                f"[WebhookNotifier] Registry 管理操作 sender={sender_id} "
                f"action=revoke-path path_id={path_id} result=registry_unavailable"
            )
            return "❌ Registry 未初始化，请检查日志。"

        success, message = registry.revoke_endpoint_by_path(path)
        log_message = (
            f"[WebhookNotifier] Registry 管理操作 sender={sender_id} "
            f"action=revoke-path path_id={path_id} "
            f"result={'success' if success else 'failed'}"
        )
        if success:
            logger.info(log_message)
            return f"✅ {message}（Endpoint Path: {path}）"
        logger.warning(log_message)
        return f"❌ {message}（Endpoint Path: {path}）"

    def _handle_admin_token_revoke_owner(
        self,
        sender_id: str,
        args: list[str],
        command_root: str = PLACEHOLDER_COMMAND_ROOTS.short,
    ) -> str:
        if len(args) < 3 or not args[0].strip() or not args[1].strip():
            logger.warning(
                f"[WebhookNotifier] Registry 管理操作 sender={sender_id} "
                "action=revoke-owner result=invalid_input"
            )
            return (
                f"❌ 用法: {command_root} admin token revoke-owner "
                "<platform_id> <owner_user_id> <名称>"
            )

        platform_id = args[0].strip()
        owner_user_id = args[1].strip()
        raw_name = " ".join(args[2:]).strip()
        if not raw_name:
            logger.warning(
                f"[WebhookNotifier] Registry 管理操作 sender={sender_id} "
                "action=revoke-owner result=empty_name"
            )
            return (
                f"❌ 用法: {command_root} admin token revoke-owner "
                "<platform_id> <owner_user_id> <名称>"
            )
        name = normalize_endpoint_name(raw_name)
        target_id = sha256(
            f"{platform_id}\x1f{owner_user_id}\x1f{name}".encode("utf-8")
        ).hexdigest()[:12]

        registry = self._ensure_registry()
        if not registry:
            logger.error(
                f"[WebhookNotifier] Registry 管理操作 sender={sender_id} "
                f"action=revoke-owner target_id={target_id} "
                "result=registry_unavailable"
            )
            return "❌ Registry 未初始化，请检查日志。"

        success, message = registry.revoke_endpoint_by_owner_name(
            platform_id, owner_user_id, name
        )
        log_message = (
            f"[WebhookNotifier] Registry 管理操作 sender={sender_id} "
            f"action=revoke-owner target_id={target_id} "
            f"result={'success' if success else 'failed'}"
        )
        if success:
            logger.info(log_message)
            return f"✅ {message}（platform/owner/name: {platform_id}/{owner_user_id}/{name}）"
        logger.warning(log_message)
        return (
            f"❌ {message}（platform/owner/name: {platform_id}/{owner_user_id}/{name}）"
        )

    async def _handle_token_command(
        self,
        event: AstrMessageEvent,
        args: list[str],
        commands: _CommandRoots = PLACEHOLDER_COMMAND_ROOTS,
    ) -> Any:
        """处理 /whn token 子命令。"""
        if not args:
            return (
                "用法:\n"
                f"  {commands.short} token new private [名称]\n"
                f"  {commands.short} token new group <数字群号> [名称]  (aiocqhttp)\n"
                f"  {commands.short} token new group current [名称]  (qq_official)\n"
                f"  {commands.short} token verify <request_id> <code>\n"
                f"  {commands.short} token confirm <request_id>  (qq_official)\n"
                f"  {commands.short} token list\n"
                f"  {commands.short} token rotate <名称>\n"
                f"  {commands.short} token revoke <名称>\n"
                f"  {commands.short} token delete <名称>\n\n"
                "aiocqhttp：原申请者须在预指定群以群主/管理员身份 verify，随后私聊 rotate。\n"
                "qq_official：当前群任一群主/管理员可 verify；原申请者随后同 Bot 私聊 confirm。\n"
                "revoke 保留审计记录；delete 仅永久删除 revoked/expired 终态记录。"
            )

        action = args[0].lower()

        if action == "new":
            return await self._handle_token_new(event, args[1:], commands)
        elif action == "verify":
            return await self._handle_token_verify(event, args[1:], commands)
        elif action == "confirm":
            return await self._handle_token_confirm(event, args[1:], commands)
        elif action == "list":
            return self._handle_token_list(event, commands)
        elif action == "rotate":
            return await self._handle_token_rotate(event, args[1:], commands)
        elif action == "revoke":
            return await self._handle_token_revoke(event, args[1:], commands)
        elif action == "delete":
            return await self._handle_token_delete(event, args[1:], commands)
        else:
            return (
                f"❌ 未知 token 子命令：{action}\n"
                f"发送 {commands.short} help 查看可用命令。"
            )

    # ─── Token 管理 ──────────────────────────────────────────

    async def _handle_token_new(
        self,
        event: AstrMessageEvent,
        args: list[str],
        commands: _CommandRoots = PLACEHOLDER_COMMAND_ROOTS,
    ) -> list[str] | str:
        """处理 /whn token new 命令。"""
        # 必须是私聊
        if not self._is_private(event):
            return "❌ 创建 endpoint 必须在私聊中执行。"

        if not args:
            return (
                "用法:\n"
                f"  {commands.short} token new private [名称]\n"
                f"  {commands.short} token new group <数字群号> [名称]  (aiocqhttp)\n"
                f"  {commands.short} token new group current [名称]  (qq_official)"
            )

        # 解析可选的 --provider 参数，未指定时默认 omp
        provider, filtered_args = self._extract_provider_flag(args)
        if not provider:
            provider = "omp"

        mode = filtered_args[0].lower() if filtered_args else ""
        name_param = (
            " ".join(filtered_args[1:]).strip() if len(filtered_args) > 1 else ""
        )

        # 验证 provider 值
        provider_ok, provider_msg = self._validate_create_provider(provider)
        if not provider_ok:
            return provider_msg

        if mode == "private":
            return await self._create_private_endpoint(
                event, name_param, provider=provider
            )
        elif mode == "group":
            return await self._create_group_pending(
                event, filtered_args[1:], commands, provider=provider
            )
        else:
            return (
                f"未知类型: {mode}\n"
                f"用法: {commands.short} token new private [名称] [--provider <omp|opencode>]；"
                "群聊请按当前平台使用数字群号或 current"
            )

    @staticmethod
    def _extract_provider_flag(
        args: list[str],
    ) -> tuple[str, list[str]]:
        """从命令参数中提取 ``--provider <value>``。

        重复 ``--provider`` 视为参数错误，返回空 provider 让调用方处理拒绝。
        末尾缺值（如 ``--provider`` 后无参数）也视为空 provider。

        Returns:
            (provider_value, remaining_args)
            未提供时 provider_value 为空字符串。
        """
        result_args: list[str] = []
        provider = ""
        found_provider = False
        skip_next = False
        for i, arg in enumerate(args):
            if skip_next:
                skip_next = False
                continue
            if arg == "--provider":
                if i + 1 >= len(args):
                    # 末尾缺值：标记找到但无值，跳过该 flag
                    found_provider = True
                    continue
                if found_provider:
                    # 重复 --provider，返回空 provider 让调用方拒绝
                    return "", result_args + args[i:]
                found_provider = True
                provider = args[i + 1].strip()
                skip_next = True
            else:
                result_args.append(arg)
        return provider, result_args

    def _validate_create_provider(self, provider: str) -> tuple[bool, str]:
        """验证创建时指定的 provider 值是否可用。

        Returns:
            (is_valid, error_or_empty_message)
        """
        if provider not in ("omp", "opencode"):
            return (
                False,
                f"❌ 不支持的 provider: {provider}。当前支持的 provider: omp, opencode",
            )
        # 查询 Registry 确认 adapter 是否已注册
        reg = self._ensure_provider_registry()
        if reg is None:
            return False, "❌ ProviderRegistry 未初始化，请检查日志。"
        if reg.get(provider) is None:
            msg = (
                f"❌ Provider adapter 尚未注册: {provider}。"
                "该 provider 可能在后续版本中可用。"
            )
            return False, msg
        return True, ""

    def _ensure_provider_registry(self) -> ProviderRegistry | None:
        return self._provider_registry

    def _init_default_provider_registry(self) -> None:
        """构造时初始化默认 ProviderRegistry 并冻结（含 OMP + OpenCode adapter）。"""
        reg = ProviderRegistry()
        reg.register(OmpProviderAdapter())
        reg.register(OpenCodeProviderAdapter())
        reg.freeze()
        self._provider_registry = reg

    async def _create_private_endpoint(
        self,
        event: AstrMessageEvent,
        name_param: str,
        provider: str = "omp",
    ) -> list[str] | str:
        """创建私聊 endpoint。"""
        registry = self._ensure_registry()
        if not registry:
            return "❌ Registry 未初始化，请检查日志。"

        owner_id = event.get_sender_id()
        platform_id = event.get_platform_id()
        target_umo = event.unified_msg_origin

        endpoint_name = normalize_endpoint_name(name_param or "private")

        try:
            record, token = registry.create_private_endpoint(
                owner_platform_id=platform_id,
                name=endpoint_name,
                owner_user_id=owner_id,
                target_umo=target_umo,
                description=f"私聊 endpoint for {owner_id}",
                provider=provider,
            )
        except Exception as e:
            logger.error(f"[WebhookNotifier] 创建私聊 endpoint 失败: {e}")
            return "❌ 创建失败，请检查插件日志。"
        endpoint_path = record.path

        # 如果 HTTP 服务未运行，启动
        await self._ensure_server_running()

        private_notification_notice = ""
        if not bool(self.config.get("enable_private_notifications", False)):
            private_notification_notice = (
                "\n\nℹ️ 当前 Webhook 私聊状态通知已关闭：endpoint 和 Token 仍有效，"
                "但 Webhook 通知会返回 skipped。请在配置中开启 "
                "enable_private_notifications 并 reload 后恢复。"
            )

        summary = (
            "✅ 私聊 endpoint 创建成功\n"
            f"名称: {endpoint_name}\n"
            f"Provider: {record.provider}\n"
            f"Endpoint Path: {endpoint_path}\n"
            "Base URL：请在 Plugin Page 中复制"
            f"{private_notification_notice}"
        )
        return [summary, _TokenDeliveryText(f"Bearer Token: {token}", endpoint_name)]

    async def _create_group_pending(
        self,
        event: AstrMessageEvent,
        args: list[str],
        commands: _CommandRoots = PLACEHOLDER_COMMAND_ROOTS,
        provider: str = "omp",
    ) -> str:
        """创建群聊待验证 endpoint。"""
        registry = self._ensure_registry()
        if not registry:
            return "❌ Registry 未初始化，请检查日志。"

        owner_id = event.get_sender_id()
        platform_id = event.get_platform_id()
        adapter_kind = self._get_adapter_kind(event)
        if adapter_kind == "aiocqhttp":
            if not args:
                return (
                    f"❌ 请指定 QQ 群号: {commands.short} token new group "
                    "<数字群号> [名称]"
                )
            group_id = args[0].strip()
            if not group_id.isdigit():
                return "❌ aiocqhttp 群号必须是数字。"
            binding_mode = PREBOUND_GROUP
            target_group_id: str | None = group_id
            name_param = " ".join(args[1:]).strip() if len(args) > 1 else ""
            endpoint_name = normalize_endpoint_name(name_param or f"group_{group_id}")
        elif adapter_kind == "qq_official":
            if not args or args[0].lower() != "current":
                return (
                    f"❌ QQ 官方平台用法: {commands.short} "
                    "token new group current [名称]"
                )
            binding_mode = BIND_CURRENT_GROUP
            target_group_id = None
            name_param = " ".join(args[1:]).strip() if len(args) > 1 else ""
            endpoint_name = normalize_endpoint_name(name_param or "group_current")
        else:
            return "❌ 当前平台不支持群聊 endpoint 验证。"

        try:
            record, request_id, code = registry.create_group_pending(
                owner_platform_id=platform_id,
                name=endpoint_name,
                owner_user_id=owner_id,
                group_binding_mode=binding_mode,
                target_group_id=target_group_id,
                description=f"群聊 endpoint for {owner_id}",
                provider=provider,
            )
        except Exception as e:
            logger.error(f"[WebhookNotifier] 创建群聊待验证 endpoint 失败: {e}")
            return "❌ 创建失败，请检查插件日志。"
        endpoint_path = record.path

        target_line = (
            f"目标群: {target_group_id}\n"
            if binding_mode == PREBOUND_GROUP
            else "目标群: 验证时绑定当前群\n"
        )
        binding_notice = (
            "请在目标群中由群主或群管理员执行以下命令批准当前群。\n"
            if binding_mode == BIND_CURRENT_GROUP
            else "请在目标群中发送以下命令完成验证：\n"
        )
        completion_notice = (
            "群管理员批准后，请由原申请者在同一 QQ 官方 Bot 私聊执行消息中提示的 confirm 命令。"
            if binding_mode == BIND_CURRENT_GROUP
            else f"验证通过后，请由创建者主动私聊执行 {commands.short} token rotate {endpoint_name} 领取新 Token。"
        )
        return (
            f"✅ 待验证申请已创建\n\n"
            f"名称: {endpoint_name}\n"
            f"Provider: {record.provider}\n"
            f"{target_line}"
            f"Endpoint Path: {endpoint_path}\n"
            f"请求 ID: {request_id}\n"
            f"验证码: {code}\n"
            f"有效期: 10 分钟\n\n"
            f"{binding_notice}"
            f"  {commands.short} token verify {request_id} {code}\n\n"
            f"{completion_notice}"
        )

    async def _handle_token_verify(
        self,
        event: AstrMessageEvent,
        args: list[str],
        commands: _CommandRoots = PLACEHOLDER_COMMAND_ROOTS,
    ) -> str:
        """处理 /whn token verify 命令。"""
        registry = self._ensure_registry()
        if not registry:
            return "❌ Registry 未初始化，请检查日志。"

        # 必须在群聊中执行
        if not self._is_group(event):
            return "❌ 验证命令必须在目标群中执行。"

        if len(args) < 2:
            return f"❌ 用法: {commands.short} token verify <request_id> <code>"

        request_id = args[0].strip()
        code = args[1].strip()
        platform_id = event.get_platform_id()
        pending_info = registry.get_pending_descriptor(platform_id, request_id)
        if pending_info is None:
            return "❌ 验证请求不存在或已过期"

        adapter_kind = self._get_adapter_kind(event)
        if adapter_kind == "aiocqhttp":
            stable_owner_user_id = event.get_sender_id()
            verify_group_id = event.get_group_id()
            if not verify_group_id:
                return "❌ 无法获取当前群 ID，请确认 Bot 已在群内。"
            verified_role = await self._check_aiocqhttp_group_role(event)
        elif adapter_kind == "qq_official":
            context = self._get_qq_official_verification_context(event)
            if context is None:
                return "❌ 当前平台无法校验群管理员身份，群聊 token 验证失败。"
            verify_group_id, verified_role = context
            stable_owner_user_id = None
        else:
            return "❌ 当前平台无法校验群管理员身份，群聊 token 验证失败。"
        if verified_role is None:
            return "❌ 当前平台无法校验群管理员身份，群聊 token 验证失败。"

        result_status, result_msg, _ = registry.verify_group_endpoint(
            owner_platform_id=platform_id,
            request_id=request_id,
            code=code,
            stable_owner_user_id=stable_owner_user_id,
            group_id=str(verify_group_id),
            verified_role=verified_role,
        )

        if result_status == "ok":
            await self._ensure_server_running()

            # 获取 endpoint 信息。verify_group_endpoint 会清理 pending，
            # 因此这里使用验证前复制出的 pending_info。
            endpoint_name = pending_info.get("endpoint_name", "unknown")
            return (
                "✅ 群聊 endpoint 验证成功！\n"
                f"名称: {endpoint_name}\n"
                "Token 尚未发放。请由创建者主动私聊执行 "
                f"{commands.short} token rotate {endpoint_name} 领取新 Token。"
            )
        elif result_status == "waiting_owner":
            endpoint_name = pending_info.get("endpoint_name", "unknown")
            return (
                "✅ 群管理员验证成功！\n"
                f"名称: {endpoint_name}\n"
                "Endpoint 仍在等待原申请者确认，尚未生成 Token。\n"
                "请原申请者在同一 QQ 官方 Bot 私聊执行：\n"
                f"  {commands.short} token confirm {request_id}"
            )
        else:
            return f"❌ {result_msg}"

    async def _handle_token_confirm(
        self,
        event: AstrMessageEvent,
        args: list[str],
        commands: _CommandRoots = PLACEHOLDER_COMMAND_ROOTS,
    ) -> list[str] | str:
        """由 QQ 官方原 C2C 申请者私聊完成群绑定并领取 Token。"""
        registry = self._ensure_registry()
        if not registry:
            return "❌ Registry 未初始化，请检查日志。"
        if not self._is_private(event):
            return "❌ confirm 必须由原申请者在同一 Bot 私聊中执行。"
        if len(args) != 1 or not args[0].strip():
            return f"❌ 用法: {commands.short} token confirm <request_id>"

        request_id = args[0].strip()
        platform_id = event.get_platform_id()
        pending_info = registry.get_pending_descriptor(platform_id, request_id)
        if pending_info is None:
            return "❌ 验证请求不存在或已过期"

        status, message, record, token = registry.confirm_group_endpoint(
            platform_id,
            request_id,
            event.get_sender_id(),
        )
        if status != "ok" or record is None or token is None:
            return f"❌ {message}"

        await self._ensure_server_running()
        summary = (
            "✅ QQ 官方群聊 endpoint 确认成功\n"
            f"名称: {record.name}\n"
            f"Endpoint Path: {record.path}\n"
            "Base URL：请在 Plugin Page 中复制"
        )
        return [summary, _TokenDeliveryText(f"Bearer Token: {token}", record.name)]

    def _handle_token_list(
        self,
        event: AstrMessageEvent,
        commands: _CommandRoots = PLACEHOLDER_COMMAND_ROOTS,
    ) -> str:
        """列出用户的所有 endpoint。"""
        registry = self._ensure_registry()
        if not registry:
            return "❌ Registry 未初始化，请检查日志。"

        # 必须是私聊
        if not self._is_private(event):
            return "❌ 查看 endpoint 列表请在私聊中执行。"

        owner_id = event.get_sender_id()
        platform_id = event.get_platform_id()
        records = registry.list_visible_by_owner(platform_id, owner_id)

        if not records:
            return (
                "📋 您当前没有可用的 endpoint。\n"
                "已撤销或已过期的 endpoint 不在默认列表中显示。\n"
                f"使用 {commands.short} token new private [名称] 创建。"
            )

        lines = ["📋 您的 Endpoint 列表:\n"]
        for rec in records:
            status_emoji = {
                EndpointStatus.ACTIVE.value: "🟢",
                EndpointStatus.PENDING_VERIFICATION.value: "🟡",
                EndpointStatus.EXPIRED.value: "🔴",
                EndpointStatus.REVOKED.value: "⚫",
            }.get(rec.status, "⚪")

            targets_str = (
                ", ".join(t.name for t in rec.targets) if rec.targets else "无"
            )

            lines.append(
                f"{status_emoji} {rec.name}\n"
                f"   Provider: {rec.provider}\n"
                f"   Endpoint Path: {rec.path}\n"
                f"   状态: {rec.status}\n"
                f"   目标: {targets_str}\n"
                f"   创建: {rec.created_at[:19]}\n"
            )
            if rec.revoked_at:
                lines.append(f"   撤销: {rec.revoked_at[:19]}\n")

        return "\n".join(lines).strip()

    async def _handle_token_rotate(
        self,
        event: AstrMessageEvent,
        args: list[str],
        commands: _CommandRoots = PLACEHOLDER_COMMAND_ROOTS,
    ) -> list[str] | str:
        """轮换 token。"""
        registry = self._ensure_registry()
        if not registry:
            return "❌ Registry 未初始化，请检查日志。"

        if not self._is_private(event):
            return "❌ 轮换 token 请在私聊中执行。"

        if not args:
            return f"❌ 请指定 endpoint 名称: {commands.short} token rotate <名称>"

        name = normalize_endpoint_name(" ".join(args))
        owner_id = event.get_sender_id()
        platform_id = event.get_platform_id()

        success, result = registry.rotate_token(platform_id, owner_id, name)
        if not success:
            return f"❌ {result}"

        record = registry.get_by_owner_name(platform_id, owner_id, name)
        endpoint_path = record.path if record else name
        summary = (
            "✅ Token 已轮换\n"
            f"名称: {name}\n"
            f"Endpoint Path: {endpoint_path}\n"
            "⚠️ 旧 Token 已立即失效。请更新外部系统配置。"
        )
        return [summary, _TokenDeliveryText(f"Bearer Token: {result}", name)]

    async def _handle_token_revoke(
        self,
        event: AstrMessageEvent,
        args: list[str],
        commands: _CommandRoots = PLACEHOLDER_COMMAND_ROOTS,
    ) -> str:
        """撤销 endpoint。"""
        registry = self._ensure_registry()
        if not registry:
            return "❌ Registry 未初始化，请检查日志。"

        if not self._is_private(event):
            return "❌ 撤销 endpoint 请在私聊中执行。"

        if not args:
            return f"❌ 请指定 endpoint 名称: {commands.short} token revoke <名称>"

        name = normalize_endpoint_name(" ".join(args))
        owner_id = event.get_sender_id()
        platform_id = event.get_platform_id()

        success, message = registry.revoke_endpoint(platform_id, owner_id, name)
        if not success:
            return f"❌ {message}"

        return f"✅ {message}"

    async def _handle_token_delete(
        self,
        event: AstrMessageEvent,
        args: list[str],
        commands: _CommandRoots = PLACEHOLDER_COMMAND_ROOTS,
    ) -> str:
        """永久删除用户自己的 revoked/expired endpoint。"""
        if not self._is_private(event):
            return "❌ 永久删除 endpoint 请在私聊中执行。"
        if not args:
            return f"❌ 请指定 endpoint 名称: {commands.short} token delete <名称>"

        registry = self._ensure_registry()
        if not registry:
            return "❌ Registry 未初始化，请检查日志。"

        name = normalize_endpoint_name(" ".join(args))
        platform_id = event.get_platform_id()
        try:
            result, status = registry.delete_endpoint(
                platform_id, event.get_sender_id(), name
            )
        except Exception as exc:
            logger.error(
                "[WebhookNotifier] endpoint delete audit "
                f"operation=delete result=error platform={platform_id} "
                f"error={type(exc).__name__}"
            )
            return "❌ 永久删除失败，请检查插件日志。"

        log_message = (
            "[WebhookNotifier] endpoint delete audit "
            f"operation=delete result={result} status={status or 'none'} "
            f"platform={platform_id}"
        )
        if result == "deleted":
            logger.info(log_message)
            return (
                "✅ Endpoint 已永久删除\n"
                f"名称: {name}\n"
                "⚠️ 此操作不可恢复；原 Token 已失效。"
            )

        logger.warning(log_message)
        if result == "active":
            return (
                "❌ active endpoint 不能永久删除。请先执行 "
                f"{commands.short} token revoke {name}。"
            )
        if result == "pending":
            return (
                "❌ pending_verification endpoint 不能永久删除。"
                "请先完成验证、等待过期，或执行 revoke。"
            )
        if result == "not_found":
            return "❌ endpoint 不存在（可能已永久删除）。"
        return "❌ endpoint 状态不支持永久删除。"

    # ─── 辅助方法 ───────────────────────────────────────────

    def _ensure_registry(self) -> EndpointRegistry | None:
        return self._registry

    async def _ensure_server_running(self) -> None:
        """如果 HTTP 服务未运行，则启动。"""
        if self._server and self._server.running:
            return
        await self._start_server()

    async def _start_server(self) -> None:
        """启动 HTTP 服务。"""
        if not self._registry or not self._sender or not self._server_config:
            logger.error("[WebhookNotifier] 无法启动 HTTP 服务：组件未初始化")
            return
        try:
            self._server = WebhookServer(
                config=self._server_config,
                registry=self._registry,
                sender=self._sender,
                html_render=self.html_render,
                plugin_config=dict(self.config),
                template_registry=self._template_registry,
                provider_registry=self._provider_registry,
            )
            await self._server.start()
        except Exception as e:
            logger.error(f"[WebhookNotifier] 启动 HTTP 服务失败: {e}")

    def _get_render_mode(self) -> str:
        mode = self.config.get("render_mode", "text")
        if mode == "html_image":
            logger.info("[WebhookNotifier] render_mode 配置为 html_image（MS2 支持）。")
            return "html_image"
        return mode

    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        """检查是否为私聊消息。"""
        return event.session.message_type == MessageType.FRIEND_MESSAGE

    @staticmethod
    def _is_group(event: AstrMessageEvent) -> bool:
        """检查是否为群聊消息。"""
        return event.session.message_type == MessageType.GROUP_MESSAGE

    @staticmethod
    def _get_adapter_kind(event: AstrMessageEvent) -> str:
        try:
            value = event.get_platform_name()
        except (AttributeError, TypeError, NotImplementedError):
            return ""
        return value if isinstance(value, str) else ""

    async def _check_aiocqhttp_group_role(self, event: AstrMessageEvent) -> str | None:
        """仅通过 aiocqhttp 群资料判断 owner/admin/member。"""
        try:
            group_info = await event.get_group()
        except Exception as exc:
            if not self._is_controlled_aiocqhttp_error(exc):
                raise
            logger.warning(
                "[WebhookNotifier] aiocqhttp 群资料查询失败 "
                f"platform={event.get_platform_id()} error={type(exc).__name__}"
            )
            return None
        if group_info is None:
            return None

        sender_id = event.get_sender_id()
        owner_id = (
            getattr(group_info, "group_owner", None)
            or getattr(group_info, "owner", None)
            or getattr(group_info, "owner_id", None)
        )
        if owner_id and str(owner_id) == str(sender_id):
            return "owner"

        admins = (
            getattr(group_info, "group_admins", None)
            or getattr(group_info, "admins", None)
            or getattr(group_info, "admin_ids", None)
        )
        if admins and isinstance(admins, (list, tuple)):
            for admin in admins:
                admin_id = (
                    admin
                    if isinstance(admin, (str, int))
                    else getattr(admin, "user_id", None) or getattr(admin, "id", None)
                )
                if admin_id and str(admin_id) == str(sender_id):
                    return "admin"
        return "member"

    @staticmethod
    def _is_controlled_aiocqhttp_error(exc: Exception) -> bool:
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return True
        error_type = type(exc)
        return error_type.__name__ in {"ActionFailed", "NetworkError"} and (
            "aiocqhttp" in error_type.__module__.lower()
        )

    @staticmethod
    def _get_qq_official_verification_context(
        event: AstrMessageEvent,
    ) -> tuple[str, str] | None:
        """仅从 QQ 官方群消息 raw_data 提取群与管理员角色。"""
        try:
            raw_data = event.message_obj.raw_message.raw_data
        except AttributeError:
            return None
        if not isinstance(raw_data, dict):
            return None
        group_openid = raw_data.get("group_openid")
        author = raw_data.get("author")
        if not isinstance(group_openid, str) or not group_openid.strip():
            return None
        if not isinstance(author, dict):
            return None
        member_openid = author.get("member_openid")
        member_role = author.get("member_role")
        if not isinstance(member_openid, str) or not member_openid.strip():
            return None
        if not isinstance(member_role, str) or member_role not in {
            "owner",
            "admin",
            "member",
        }:
            return None
        return group_openid, member_role

    # ─── 状态构建 ───────────────────────────────────────────

    def _build_status_text(
        self, commands: _CommandRoots = PLACEHOLDER_COMMAND_ROOTS
    ) -> _SafeStatusText:
        enabled = self._enabled
        server_running = self._server is not None and self._server.running
        active_count = self._registry.count_deliverable() if self._registry else 0
        render_mode = self._get_render_mode()
        fallback = self.config.get("fallback_to_text", True)
        private_notifications = bool(
            self.config.get("enable_private_notifications", False)
        )
        host = self._server_config.host if self._server_config else "127.0.0.1"
        port = self._server_config.port if self._server_config else 18080
        base_path = self._server_config.base_path if self._server_config else "/webhook"

        # 检查早期占位 token。仅兼容用户本地旧配置，不在新配置界面展示。
        legacy_token = bool(str(self.config.get("webhook_token", "")).strip())

        lines = [
            f"{PLUGIN_NAME} 状态",
            "",
            f"启用状态：{'✅ 已启用' if enabled else '❌ 已禁用'}",
            f"HTTP 服务：{'✅ 运行中' if server_running else '⏸ 未启动'}",
            f"Active Endpoint：{active_count} 个",
            f"渲染模式：{render_mode}",
            f"渲染降级：{'开启' if fallback else '关闭'}",
            f"Webhook 私聊状态通知：{'开启' if private_notifications else '关闭'}",
            f"监听 IP：{host}",
            f"监听端口：{port}",
            f"基础路径：{base_path}",
            "Base URL：请在 Plugin Page 中复制",
        ]

        if legacy_token:
            lines.append("")
            lines.append("⚠️ 检测到早期占位 webhook_token 配置（MVP 未启用）")
            lines.append(
                f"   请使用 {commands.short} token new 命令创建 managed endpoint。"
            )

        if commands.config_error:
            lines.extend(("", f"⚠️ {WAKE_PREFIX_DIAGNOSTIC}"))

        lines.extend(
            [
                "",
                "可用命令：",
                f"  {commands.long}  查看完整状态",
                f"  {commands.short}  查看状态 / 管理 endpoint",
            ]
        )

        return _SafeStatusText("\n".join(lines))

    @property
    def _enabled(self) -> bool:
        return bool(self.config.get("enabled", True))
