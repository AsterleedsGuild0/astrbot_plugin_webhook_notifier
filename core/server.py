from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, NoReturn

from aiohttp import web

from astrbot.api import logger

from .display import DEFAULT_DISPLAY_TIMEZONE, create_display_context
from .idempotency import (
    ClaimResult,
    ClaimStatus,
    IdempotencyStore,
    make_idempotency_key,
)
from .models import DisplayContext, EndpointRecord, NormalizedEvent, ServerConfig
from .notification_policy import normalize_notification_mode, should_notify
from .providers import ProviderError, ProviderRegistry, ProviderRegistryError
from .registry import EndpointRegistry
from .renderer import (
    DEFAULT_HTML_TEMPLATE,
    PreparedSubagentTimeline,
    SUBAGENT_TIMELINE_MAX_TIMEOUT_MS,
    prepare_subagent_timeline,
    render_html_data,
    render_html_template,
    render_subagent_timeline,
    render_text_default,
    trim_viewport_whitespace,
    validate_image_result,
)
from .template_registry import BUILT_IN_ID, ActiveTemplate, TemplateRegistry
from .sender import DeliveryAttemptTracker, Sender

DEFAULT_RENDER_OPTIONS: dict[str, Any] = {
    "full_page": True,
    "type": "png",
    "quality": 90,
    "timeout": 5000,
    "viewport_width": 812,
    "viewport_height": 1200,
    "device_scale_factor_level": "high",
    "wait_until": "domcontentloaded",
}

_RENDER_FAILURE_MESSAGES: dict[str, str] = {
    "render_failed": "HTML 图片渲染失败",
    "html_render_not_available": "HTML 渲染不可用",
    "template_render_failed": "HTML 模板渲染失败",
    "html_render_failed": "HTML 图片截图失败",
    "image_validation_failed": "图片结果校验失败",
    "image_trim_failed": "图片裁切失败",
    "timeline_render_failed": "时间线图片渲染失败",
    "timeline_template_render_failed": "时间线模板渲染失败",
    "timeline_html_render_failed": "时间线图片截图失败",
    "timeline_image_validation_failed": "时间线图片校验失败",
    "timeline_image_trim_failed": "时间线图片裁切失败",
    "send_image_failed": "发送图片失败",
}


def _safe_failure_reason(error: BaseException, default: str = "render_failed") -> str:
    reason = getattr(error, "reason", None)
    if isinstance(reason, str) and reason in _RENDER_FAILURE_MESSAGES:
        return reason
    return default


def _safe_exception_type(error: BaseException) -> str:
    exception_type = getattr(error, "exception_type", None)
    if isinstance(exception_type, str) and exception_type:
        return exception_type
    return type(error).__name__


def _safe_failure_message(reason: str) -> str:
    return _RENDER_FAILURE_MESSAGES.get(
        reason, _RENDER_FAILURE_MESSAGES["render_failed"]
    )


def _raise_render_failure(
    reason: str,
    message: str,
    cause: BaseException | None = None,
) -> NoReturn:
    error = RuntimeError(message)
    error.reason = reason  # type: ignore[attr-defined]
    error.exception_type = type(cause).__name__ if cause is not None else "RuntimeError"  # type: ignore[attr-defined]
    if cause is None:
        raise error
    raise error from cause


class WebhookServer:
    """Webhook HTTP Server。

    使用 aiohttp.web，提供 POST /webhook/{endpoint} 端点。

    支持 text 和 html_image 两种渲染模式。
    html_image 模式使用 AstrBot html_render 回调生成图片，
    失败时可按 fallback_to_text 降级为纯文本。
    """

    def __init__(
        self,
        config: ServerConfig,
        registry: EndpointRegistry,
        sender: Sender,
        html_render: Callable | None = None,
        plugin_config: dict[str, Any] | None = None,
        template_registry: TemplateRegistry | None = None,
        provider_registry: ProviderRegistry | None = None,
        display_context: DisplayContext | None = None,
        idempotency_store: IdempotencyStore | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._sender = sender
        self._html_render: Callable | None = html_render
        self._plugin_config: dict[str, Any] = plugin_config or {}
        self._template_registry = template_registry
        self._provider_registry = provider_registry or ProviderRegistry()
        self._display_context = display_context or create_display_context(
            self._plugin_config.get("display_timezone", DEFAULT_DISPLAY_TIMEZONE),
            warn=logger.warning,
        )
        self._idempotency_store = idempotency_store or IdempotencyStore()
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        """启动 HTTP 服务。"""
        self._app = web.Application()

        # 注册 POST 路由
        base = self._config.base_path.rstrip("/")
        self._app.router.add_post(f"{base}/{{tail:.*}}", self._handle_webhook)

        # 健康检查
        self._app.router.add_get(f"{base}/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            self._config.host,
            self._config.port,
        )
        await self._site.start()
        logger.info(
            f"[WebhookNotifier] HTTP 服务已启动: "
            f"http://{self._config.host}:{self._config.port}{base}"
        )

    async def stop(self) -> None:
        """停止 HTTP 服务。"""
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        logger.info("[WebhookNotifier] HTTP 服务已停止")

    @property
    def running(self) -> bool:
        return self._site is not None

    async def _handle_health(self, request: web.Request) -> web.Response:
        """健康检查端点。"""
        return web.json_response(
            {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}
        )

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """处理 Webhook 请求的主入口。"""
        request_id = str(request.headers.get("X-Request-ID", ""))
        if not request_id:
            import uuid

            request_id = str(uuid.uuid4())

        try:
            return await self._process_request(request, request_id)
        except web.HTTPException:
            raise
        except Exception as exc:
            logger.error(
                f"[WebhookNotifier] request_id={request_id} "
                "reason=internal_error "
                f"type={type(exc).__name__}"
            )
            return self._error_response(
                500,
                "internal_error",
                "服务器内部错误",
                request_id,
                retryable=True,
            )

    # ─── 内部辅助方法 ────────────────────────────────────────

    def _get_render_mode(self) -> str:
        """读取插件全局渲染模式。

        MVP 阶段 render_mode 为全局配置，所有 endpoint 共享。
        endpoint 级渲染模式已在 EndpointRecord 中移除。
        """
        pm = str(self._plugin_config.get("render_mode", "text"))
        return pm if pm in ("text", "html_image") else "text"

    def _get_fallback_to_text(self) -> bool:
        return bool(self._plugin_config.get("fallback_to_text", True))

    def _get_notification_mode(self) -> str:
        """读取全局通知降噪模式。

        缺失配置使用 focused；运行时非法值 fail-open 为 all。
        """
        if "notification_mode" not in self._plugin_config:
            return normalize_notification_mode()
        return normalize_notification_mode(self._plugin_config.get("notification_mode"))

    @staticmethod
    def _session_scope_value(event: NormalizedEvent) -> str:
        scope = event.session_scope
        return scope.value if hasattr(scope, "value") else str(scope)

    def _get_render_options(self) -> dict[str, Any]:
        raw = self._plugin_config.get("render_options", "")
        if not raw:
            return dict(DEFAULT_RENDER_OPTIONS)
        if isinstance(raw, dict):
            return DEFAULT_RENDER_OPTIONS | raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return DEFAULT_RENDER_OPTIONS | parsed
            except (json.JSONDecodeError, TypeError):
                logger.warning("[WebhookNotifier] render_options 解析失败，使用默认值")
                return dict(DEFAULT_RENDER_OPTIONS)
        return dict(DEFAULT_RENDER_OPTIONS)

    # ─── 请求处理 ────────────────────────────────────────────

    async def _process_request(
        self, request: web.Request, request_id: str
    ) -> web.Response:
        # 1. 检查 Content-Type
        content_type = request.content_type or ""
        if content_type not in ("application/json",) and not content_type.endswith(
            "+json"
        ):
            return self._error_response(
                415,
                "unsupported_media_type",
                f"不支持的 Content-Type: {content_type}",
                request_id,
            )

        # 2. 检查 body 大小（Content-Length 方式）
        content_length = request.content_length
        if (
            content_length is not None
            and content_length > self._config.body_limit_bytes
        ):
            return self._error_response(
                413,
                "payload_too_large",
                f"请求体超过大小限制 ({self._config.body_limit_bytes} bytes)",
                request_id,
            )

        # 3. 读取 body（带大小限制）
        try:
            body_bytes = await request.read()
        except Exception:
            return self._error_response(
                400, "invalid_json", "读取请求体失败", request_id
            )

        if len(body_bytes) > self._config.body_limit_bytes:
            return self._error_response(
                413,
                "payload_too_large",
                f"请求体超过大小限制 ({self._config.body_limit_bytes} bytes)",
                request_id,
            )

        # 4. 解析 JSON
        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError:
            return self._error_response(
                400, "invalid_json", "JSON 解析失败", request_id
            )

        if not isinstance(body, dict):
            return self._error_response(
                400, "invalid_payload", "请求体必须是 JSON object", request_id
            )

        # 5. 提取 endpoint 路径
        base = self._config.base_path.rstrip("/")
        path = request.path
        endpoint_path = (
            path[len(base) :].lstrip("/") if path.startswith(base) else path.lstrip("/")
        )

        # 6. Registry 在单一接口中保持 header/path/status/token 的既有错误优先级。
        auth = self._registry.authenticate_delivery(
            endpoint_path, request.headers.get("Authorization")
        )
        if not auth.authorized:
            status = 404 if auth.error_code == "not_found" else 401
            if auth.error_code in {
                "token_unclaimed",
                "endpoint_revoked",
                "endpoint_disabled",
            }:
                status = 403
            logger.warning(
                f"[WebhookNotifier] request_id={request_id} "
                f"endpoint 鉴权失败: {auth.error_code}"
            )
            return self._error_response(
                status,
                auth.error_code or "invalid_token",
                auth.message,
                request_id,
            )
        endpoint = auth.record
        if endpoint is None:  # 防御性检查，authorized 结果必须携带快照。
            return self._error_response(
                500, "internal_error", "Registry 鉴权结果无效", request_id
            )

        # 7. 通过 ProviderRegistry 精确选择 adapter
        adapter = self._provider_registry.get(endpoint.provider)
        if adapter is None:
            logger.warning(
                f"[WebhookNotifier] request_id={request_id} "
                f"endpoint={endpoint.name} provider={endpoint.provider} "
                "adapter 未注册"
            )
            return self._error_response(
                500,
                "provider_unavailable",
                f"Provider adapter 未注册: {endpoint.provider}",
                request_id,
                retryable=True,
            )

        # 8. 通过 adapter 解析并标准化事件
        request_time = datetime.now(timezone.utc).isoformat()
        try:
            headers_dict = dict(request.headers)
            event = adapter.parse(
                headers=headers_dict, payload=body, received_at=request_time
            )
        except ProviderError as e:
            # 不泄露底层异常原文或 payload 片段；如需根因查看 debug 级别日志
            logger.info(
                f"[WebhookNotifier] request_id={request_id} "
                f"endpoint={endpoint.name} provider={endpoint.provider} "
                f"parse 拒绝: code={e.code}"
            )
            return self._error_response(
                400, e.code, str(e), request_id, retryable=e.retryable
            )
        except ProviderRegistryError as e:
            logger.warning(
                f"[WebhookNotifier] request_id={request_id} "
                f"endpoint={endpoint.name} provider={endpoint.provider} "
                f"registry 错误: {e}"
            )
            return self._error_response(
                500, "provider_unavailable", str(e), request_id, retryable=True
            )
        except Exception as e:
            logger.error(
                f"[WebhookNotifier] request_id={request_id} "
                f"endpoint={endpoint.name} provider={endpoint.provider} "
                f"adapter 内部异常: {type(e).__name__}"
            )
            return self._error_response(
                500,
                "internal_error",
                "Provider adapter 内部错误",
                request_id,
                retryable=True,
            )

        logger.info(
            f"[WebhookNotifier] request_id={request_id} "
            f"endpoint={endpoint.name} provider={endpoint.provider} "
            f"event={event.event}"
        )

        # 9. 解析 payload 中的 target alias。优先使用明确的兼容字段，不能用
        # truthiness fallback，否则空值会悄悄改成另一个路由选择器。
        if "target_alias" in body:
            payload_target_alias = body["target_alias"]
        else:
            payload_target_alias = body.get("target")

        return await self._dispatch_event(
            event, endpoint, payload_target_alias, request_id
        )

    async def _dispatch_event(
        self,
        event: NormalizedEvent,
        endpoint: EndpointRecord,
        target_alias: str | None,
        request_id: str,
    ) -> web.Response:
        """应用发送策略后按全局渲染模式处理已标准化事件。"""
        if target_alias is not None and (
            not isinstance(target_alias, str) or not target_alias
        ):
            return self._error_response(
                400,
                "invalid_target_alias",
                "target_alias 必须是 null 或非空字符串",
                request_id,
            )
        if target_alias is not None and not any(
            target.name == target_alias for target in endpoint.targets
        ):
            return self._error_response(
                400,
                "invalid_target_alias",
                "target_alias 未绑定到当前 endpoint",
                request_id,
            )

        notification_mode = self._get_notification_mode()
        if not should_notify(
            mode=notification_mode,
            session_scope=event.session_scope,
            status=event.status,
        ):
            logger.info(
                "[WebhookNotifier] notification filtered "
                f"provider={event.provider} event={event.event} "
                f"mode={notification_mode} scope={self._session_scope_value(event)} "
                f"status={event.status} reason=notification_mode_filtered"
            )
            return self._build_notification_mode_skip_response(
                request_id=request_id,
                provider=event.provider,
                event_name=event.event,
                scope=self._session_scope_value(event),
            )

        logger.info(
            f"event.provider={event.provider} endpoint.provider={endpoint.provider} "
            f"event={event.event}"
        )
        render_mode = self._get_render_mode()

        skipped_results = self._sender.preflight_private_notification_policy(
            endpoint, target_alias
        )
        if skipped_results is not None:
            logger.info(
                f"[WebhookNotifier] request_id={request_id} "
                f"provider={event.provider} event={event.event} result=skipped "
                "reason=private_notifications_disabled "
                f"skipped_target_count={len(skipped_results)} rendered=false"
            )
            return self._build_render_response(
                request_id=request_id,
                provider=event.provider,
                event_name=event.event,
                render_mode=render_mode,
                requested_render_mode=render_mode,
                fallback_to_text=False,
                fallback_reason=None,
                send_results=skipped_results,
                rendered=False,
            )

        # 13. 过滤与私聊 preflight 之后才进入幂等 single-flight。
        if not event.id:
            return await self._dispatch_owned_event(
                event,
                endpoint,
                target_alias,
                request_id,
                claim=None,
            )

        key = make_idempotency_key(
            endpoint_provider=endpoint.provider,
            endpoint_path=endpoint.path,
            event_name=event.event,
            event_id=event.id,
            session_scope=self._session_scope_value(event),
            target_alias=target_alias,
        )
        claim = self._idempotency_store.claim(key)
        if claim.status is ClaimStatus.CAPACITY:
            self._log_idempotency(request_id, "capacity")
            return self._error_response(
                503,
                "idempotency_capacity",
                "幂等执行容量暂时耗尽",
                request_id,
                retryable=True,
            )
        if claim.status is ClaimStatus.REPLAY:
            self._log_idempotency(request_id, "replayed")
            return self._build_idempotency_skip_response(request_id, event)
        if claim.status is ClaimStatus.WAITER:
            self._log_idempotency(request_id, "waited")
            claim = await self._idempotency_store.wait(claim)
            if claim.status is ClaimStatus.REPLAY:
                self._log_idempotency(request_id, "replayed")
                return self._build_idempotency_skip_response(request_id, event)
            if claim.status is ClaimStatus.CAPACITY:
                self._log_idempotency(request_id, "capacity")
                return self._error_response(
                    503,
                    "idempotency_capacity",
                    "幂等执行容量暂时耗尽",
                    request_id,
                    retryable=True,
                )

        self._log_idempotency(request_id, "claimed")
        return await self._dispatch_owned_event(
            event,
            endpoint,
            target_alias,
            request_id,
            claim=claim,
        )

    async def _dispatch_owned_event(
        self,
        event: NormalizedEvent,
        endpoint: EndpointRecord,
        target_alias: str | None,
        request_id: str,
        *,
        claim: ClaimResult | None,
    ) -> web.Response:
        tracker = DeliveryAttemptTracker() if claim is not None else None
        try:
            response = await self._dispatch_render_send(
                event,
                endpoint,
                target_alias,
                request_id,
                tracker,
            )
        except BaseException:
            if claim is not None:
                self._finish_idempotency_claim(
                    claim, tracker, request_id=request_id, response=None
                )
            raise

        if claim is not None:
            self._finish_idempotency_claim(
                claim, tracker, request_id=request_id, response=response
            )
        return response

    async def _dispatch_render_send(
        self,
        event: NormalizedEvent,
        endpoint: EndpointRecord,
        target_alias: str | None,
        request_id: str,
        tracker: DeliveryAttemptTracker | None,
    ) -> web.Response:
        """进入渲染/发送分支；调用方负责 owner 终态。"""
        render_mode = self._get_render_mode()
        fallback_to_text = self._get_fallback_to_text()
        if render_mode == "html_image":
            return await self._handle_html_image(
                event,
                endpoint,
                target_alias,
                request_id,
                fallback_to_text,
                tracker,
            )
        else:
            return await self._handle_text(
                event,
                endpoint,
                target_alias,
                request_id,
                tracker,
            )

    async def _send_with_attempt_tracker(
        self,
        method_name: str,
        *args: Any,
        tracker: DeliveryAttemptTracker | None,
    ) -> list[dict[str, Any]]:
        method = getattr(self._sender, method_name)
        if tracker is None:
            return await method(*args)
        return await method(*args, delivery_attempt_callback=tracker)

    def _finish_idempotency_claim(
        self,
        claim: ClaimResult,
        tracker: DeliveryAttemptTracker | None,
        *,
        request_id: str,
        response: web.Response | None,
    ) -> None:
        attempted = tracker is not None and tracker.attempted
        if attempted:
            if self._idempotency_store.finalize(claim):
                self._log_idempotency(request_id, "finalized")
        else:
            if self._idempotency_store.release(claim):
                self._log_idempotency(request_id, "released")

    def _log_idempotency(self, request_id: str, state: str) -> None:
        logger.info(f"[WebhookNotifier] request_id={request_id} idempotency={state}")

    def _build_idempotency_skip_response(
        self,
        request_id: str,
        event: NormalizedEvent,
    ) -> web.Response:
        render_mode = self._get_render_mode()
        return web.json_response(
            {
                "code": 0,
                "message": "skipped",
                "data": {
                    "request_id": request_id,
                    "provider": event.provider,
                    "event": event.event,
                    "delivered": False,
                    "targets": [],
                    "render_mode": render_mode,
                    "requested_render_mode": render_mode,
                    "fallback_to_text": False,
                    "fallback_reason": None,
                    "skipped": True,
                    "skip_reason": "idempotency_replay",
                    "deduplicated": True,
                    "rendered": False,
                    "retryable": False,
                },
            }
        )

    async def _handle_text(
        self,
        event: NormalizedEvent,
        endpoint: EndpointRecord,
        target_alias: str | None,
        request_id: str,
        tracker: DeliveryAttemptTracker | None = None,
    ) -> web.Response:
        """纯文本渲染与发送。"""
        try:
            rendered = render_text_default(event, self._display_context)
        except Exception as exc:
            logger.error(
                f"[WebhookNotifier] request_id={request_id} "
                "reason=text_render_failed "
                f"type={type(exc).__name__}"
            )
            return self._error_response(
                500, "render_failed", "文本渲染失败", request_id
            )

        send_results = await self._send_with_attempt_tracker(
            "send_text",
            rendered,
            endpoint,
            target_alias,
            tracker=tracker,
        )
        return self._build_render_response(
            request_id=request_id,
            provider=event.provider,
            event_name=event.event,
            render_mode="text",
            requested_render_mode="text",
            fallback_to_text=False,
            fallback_reason=None,
            send_results=send_results,
        )

    async def _handle_html_image(
        self,
        event: NormalizedEvent,
        endpoint: EndpointRecord,
        target_alias: str | None,
        request_id: str,
        fallback_to_text: bool,
        tracker: DeliveryAttemptTracker | None = None,
    ) -> web.Response:
        """渲染一次 active 快照；custom 失败时在发送前尝试 built-in。"""
        active = (
            self._template_registry.get_active()
            if self._template_registry
            else ActiveTemplate(
                BUILT_IN_ID, "Built-in", DEFAULT_HTML_TEMPLATE, 812, 0, ""
            )
        )
        attempts = [active]
        if active.id != BUILT_IN_ID:
            attempts.append(
                ActiveTemplate(
                    BUILT_IN_ID, "Built-in", DEFAULT_HTML_TEMPLATE, 812, 0, ""
                )
            )
        image_result: Any = None
        render_options: dict[str, Any] = {}
        failure_reason = "render_failed"
        prepared_timeline = prepare_subagent_timeline(event)
        for template in attempts:
            try:
                image_result, render_options = await self._render_image_attempt(
                    event, template, request_id, prepared_timeline
                )
                break
            except Exception as exc:
                failure_reason = _safe_failure_reason(exc)
                logger.error(
                    f"[WebhookNotifier] request_id={request_id} "
                    f"template={template.id} reason={failure_reason} "
                    f"type={_safe_exception_type(exc)}"
                )
        else:
            if fallback_to_text:
                return await self._fallback_to_text(
                    event,
                    endpoint,
                    target_alias,
                    request_id,
                    failure_reason,
                    tracker,
                )
            return self._error_response(
                500,
                "render_failed",
                _safe_failure_message(failure_reason),
                request_id,
            )

        timeline_image: Any = None
        try:
            timeline_image = await self._render_timeline_image_attempt(
                event, request_id, prepared_timeline
            )
        except Exception as exc:
            failure_reason = _safe_failure_reason(exc, "timeline_render_failed")
            logger.warning(
                f"[WebhookNotifier] request_id={request_id} "
                f"reason={failure_reason} type={_safe_exception_type(exc)} "
                "action=main_image_only"
            )

        try:
            if timeline_image is None:
                send_results = await self._send_with_attempt_tracker(
                    "send_image",
                    image_result,
                    endpoint,
                    target_alias,
                    tracker=tracker,
                )
            else:
                send_results = await self._send_with_attempt_tracker(
                    "send_images",
                    [image_result, timeline_image],
                    endpoint,
                    target_alias,
                    tracker=tracker,
                )
        except Exception as exc:
            logger.error(
                f"[WebhookNotifier] request_id={request_id} "
                "reason=send_image_failed "
                f"type={type(exc).__name__}"
            )
            if fallback_to_text and not (tracker and tracker.attempted):
                return await self._fallback_to_text(
                    event,
                    endpoint,
                    target_alias,
                    request_id,
                    "send_image_failed",
                    tracker,
                )
            return self._error_response(
                500,
                "render_failed",
                _safe_failure_message("send_image_failed"),
                request_id,
            )

        # Phase 4b: 检查是否为图片构造失败（message_build 阶段）
        # 如果全部目标都因 unsupported_image_result 失败，属于构造失败而非真实发送失败，
        # 应触发降级而非按正常发送失败返回。
        if send_results and all(
            not r.get("ok", False) and r.get("error") == "unsupported_image_result"
            for r in send_results
        ):
            if timeline_image is not None and not (tracker and tracker.attempted):
                logger.warning(
                    f"[WebhookNotifier] request_id={request_id} "
                    "reason=image_construction_failed action=main_image_only"
                )
                try:
                    send_results = await self._send_with_attempt_tracker(
                        "send_image",
                        image_result,
                        endpoint,
                        target_alias,
                        tracker=tracker,
                    )
                except Exception as exc:
                    logger.error(
                        f"[WebhookNotifier] request_id={request_id} "
                        "reason=send_image_failed "
                        f"type={type(exc).__name__}"
                    )
                    if fallback_to_text and not (tracker and tracker.attempted):
                        return await self._fallback_to_text(
                            event,
                            endpoint,
                            target_alias,
                            request_id,
                            "send_image_failed",
                            tracker,
                        )
                    return self._error_response(
                        500,
                        "render_failed",
                        _safe_failure_message("send_image_failed"),
                        request_id,
                    )
            else:
                logger.error(
                    f"[WebhookNotifier] request_id={request_id} "
                    "reason=image_construction_failed action=fallback"
                )
                if fallback_to_text and not (tracker and tracker.attempted):
                    return await self._fallback_to_text(
                        event,
                        endpoint,
                        target_alias,
                        request_id,
                        "image_construction_failed",
                        tracker,
                    )
                return self._error_response(
                    500,
                    "render_failed",
                    "图片构造失败（不支持的图片结果类型）",
                    request_id,
                )

        return self._build_render_response(
            request_id=request_id,
            provider=event.provider,
            event_name=event.event,
            render_mode="html_image",
            requested_render_mode="html_image",
            fallback_to_text=fallback_to_text,
            fallback_reason=None,
            send_results=send_results,
        )

    async def _render_timeline_image_attempt(
        self,
        event: NormalizedEvent,
        request_id: str,
        prepared_timeline: PreparedSubagentTimeline | None = None,
    ) -> Any:
        """生成复杂时间线附图；失败由调用方安全降级为主卡。"""
        if not self._html_render:
            _raise_render_failure("html_render_not_available", "HTML 渲染不可用")

        try:
            rendered_timeline = render_subagent_timeline(event, prepared_timeline)
        except Exception as exc:
            _raise_render_failure(
                "timeline_template_render_failed", "时间线模板渲染失败", exc
            )

        if rendered_timeline is None:
            return None

        rendered_html = rendered_timeline.html
        layout = rendered_timeline.layout
        render_options = self._get_render_options()
        render_options["full_page"] = True
        render_options["viewport_width"] = layout.viewport_width
        render_options["viewport_height"] = layout.viewport_height
        configured_timeout = render_options.get(
            "timeout", DEFAULT_RENDER_OPTIONS["timeout"]
        )
        try:
            configured_timeout_ms = int(configured_timeout)
        except (TypeError, ValueError):
            configured_timeout_ms = int(DEFAULT_RENDER_OPTIONS["timeout"])
        render_options["timeout"] = min(
            SUBAGENT_TIMELINE_MAX_TIMEOUT_MS,
            max(configured_timeout_ms, layout.render_timeout_ms),
        )
        configured_scale = str(
            render_options.get("device_scale_factor_level", "high")
        ).lower()
        if layout.prefer_normal_scale and configured_scale in {"high", "ultra"}:
            render_options["device_scale_factor_level"] = "normal"
        try:
            image_result = await self._html_render(
                "{{ rendered_html | safe }}",
                {"rendered_html": rendered_html},
                return_url=False,
                options=render_options,
            )
        except Exception as exc:
            _raise_render_failure(
                "timeline_html_render_failed", "时间线图片截图失败", exc
            )

        try:
            validate_image_result(image_result)
        except Exception as exc:
            _raise_render_failure(
                "timeline_image_validation_failed", "时间线图片校验失败", exc
            )

        try:
            trimmed_image = trim_viewport_whitespace(
                image_result,
                canvas_width=layout.viewport_width,
                card_width=layout.card_width,
                body_padding=layout.body_padding,
            )
            if trimmed_image is None:
                _raise_render_failure(
                    "timeline_image_trim_failed", "时间线图片裁切失败"
                )
            return trimmed_image
        except Exception as exc:
            if _safe_failure_reason(exc) == "timeline_image_trim_failed":
                raise
            _raise_render_failure(
                "timeline_image_trim_failed", "时间线图片裁切失败", exc
            )

    async def _render_image_attempt(
        self,
        event: NormalizedEvent,
        template: ActiveTemplate,
        request_id: str,
        prepared_timeline: PreparedSubagentTimeline | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """完成单个模板的生成、截图、校验与 trim，不发送。"""
        if not self._html_render:
            _raise_render_failure("html_render_not_available", "HTML 渲染不可用")
        try:
            event_data = render_html_data(
                event, self._display_context, prepared_timeline
            )["event"]
            rendered_html = render_html_template(template.content, event_data)
        except Exception as exc:
            _raise_render_failure("template_render_failed", "HTML 模板渲染失败", exc)
        render_options = self._get_render_options()
        render_options["viewport_width"] = template.canvas_width
        try:
            image_result = await self._html_render(
                "{{ rendered_html | safe }}",
                {"rendered_html": rendered_html},
                return_url=False,
                options=render_options,
            )
        except Exception as exc:
            _raise_render_failure("html_render_failed", "HTML 图片截图失败", exc)
        try:
            validate_image_result(image_result)
        except Exception as exc:
            _raise_render_failure("image_validation_failed", "图片结果校验失败", exc)
        try:
            image_result = trim_viewport_whitespace(
                image_result, canvas_width=template.canvas_width
            )
            if image_result is None:
                _raise_render_failure("image_trim_failed", "图片裁切失败")
        except Exception as exc:
            if _safe_failure_reason(exc) == "image_trim_failed":
                raise
            _raise_render_failure("image_trim_failed", "图片裁切失败", exc)
        logger.info(
            f"[WebhookNotifier] request_id={request_id} template={template.id} "
            f"html_length={len(rendered_html)} canvas_width={template.canvas_width}"
        )
        return image_result, render_options

    async def _fallback_to_text(
        self,
        event: NormalizedEvent,
        endpoint: EndpointRecord,
        target_alias: str | None,
        request_id: str,
        fallback_reason: str,
        tracker: DeliveryAttemptTracker | None = None,
    ) -> web.Response:
        """html_image 失败后降级为纯文本。

        记录降级摘要，渲染纯文本并发送，返回带 fallback 标记的响应。
        """
        logger.warning(
            f"[WebhookNotifier] request_id={request_id} "
            f"html_image 回退到 text: {fallback_reason}"
        )

        try:
            rendered = render_text_default(event, self._display_context)
        except Exception as exc:
            logger.error(
                f"[WebhookNotifier] request_id={request_id} "
                "reason=text_fallback_render_failed "
                f"type={type(exc).__name__}"
            )
            return self._error_response(
                500, "render_failed", "降级文本渲染失败", request_id
            )

        send_results = await self._send_with_attempt_tracker(
            "send_text",
            rendered,
            endpoint,
            target_alias,
            tracker=tracker,
        )

        return self._build_render_response(
            request_id=request_id,
            provider=event.provider,
            event_name=event.event,
            render_mode="text",
            requested_render_mode="html_image",
            fallback_to_text=True,
            fallback_reason=fallback_reason,
            send_results=send_results,
        )

    @staticmethod
    def _build_render_response(
        request_id: str,
        provider: str,
        event_name: str,
        render_mode: str,
        requested_render_mode: str,
        fallback_to_text: bool,
        fallback_reason: str | None,
        send_results: list[dict[str, Any]],
        rendered: bool = True,
    ) -> web.Response:
        """构造统一渲染响应 JSON。"""
        skipped_results = [r for r in send_results if r.get("skipped", False)]
        failed_results = [r for r in send_results if not r.get("ok", False)]
        delivered = any(
            r.get("ok", False) and not r.get("skipped", False) for r in send_results
        )
        all_skipped = bool(send_results) and len(skipped_results) == len(send_results)
        target_names = [r.get("name", "unknown") for r in send_results]

        data: dict[str, Any] = {
            "request_id": request_id,
            "provider": provider,
            "event": event_name,
            "delivered": delivered,
            "targets": target_names,
            "render_mode": render_mode,
            "requested_render_mode": requested_render_mode,
            "fallback_to_text": fallback_to_text,
            "fallback_reason": fallback_reason,
            "skipped": bool(skipped_results),
            "rendered": rendered,
            "retryable": bool(failed_results),
        }

        if skipped_results:
            data["skip_reason"] = "private_notifications_disabled"

        if failed_results or skipped_results:
            data["send_results"] = send_results

        if failed_results:
            message = "partial_failure"
        elif all_skipped:
            message = "skipped"
        elif skipped_results:
            message = "partial_delivery"
        else:
            message = "ok"
        return web.json_response({"code": 0, "message": message, "data": data})

    def _build_notification_mode_skip_response(
        self,
        *,
        request_id: str,
        provider: str,
        event_name: str,
        scope: str = "unknown",
    ) -> web.Response:
        """Return the non-retryable response for a policy-filtered event."""
        render_mode = self._get_render_mode()
        return web.json_response(
            {
                "code": 0,
                "message": "skipped",
                "data": {
                    "request_id": request_id,
                    "provider": provider,
                    "event": event_name,
                    "scope": scope,
                    "delivered": False,
                    "targets": [],
                    "render_mode": render_mode,
                    "requested_render_mode": render_mode,
                    "fallback_to_text": False,
                    "fallback_reason": None,
                    "skipped": True,
                    "skip_reason": "notification_mode_filtered",
                    "reason": "notification_mode_filtered",
                    "rendered": False,
                    "retryable": False,
                },
            }
        )

    @staticmethod
    def _error_response(
        http_status: int,
        error_code: str,
        message: str,
        request_id: str,
        *,
        retryable: bool = False,
    ) -> web.Response:
        return web.json_response(
            {
                "code": 1,
                "message": message,
                "data": {
                    "request_id": request_id,
                    "error": error_code,
                    "retryable": retryable,
                },
            },
            status=http_status,
        )
