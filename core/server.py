from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from aiohttp import web

from astrbot.api import logger

from .models import EndpointRecord, NormalizedEvent, ServerConfig
from .omp import is_omp_session_stop, normalize_omp_payload
from .registry import EndpointRegistry
from .renderer import (
    render_html_default,
    render_text_default,
    validate_image_result,
)
from .security import verify_token
from .sender import Sender


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
    ) -> None:
        self._config = config
        self._registry = registry
        self._sender = sender
        self._html_render: Callable | None = html_render
        self._plugin_config: dict[str, Any] = plugin_config or {}
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
        except Exception as e:
            logger.error(
                f"[WebhookNotifier] request_id={request_id} 未预期的处理错误: {e}"
            )
            return self._error_response(500, "internal_error", str(e), request_id)

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

    def _get_render_options(self) -> dict[str, Any] | None:
        raw = self._plugin_config.get("render_options", "")
        if not raw:
            return None
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.warning("[WebhookNotifier] render_options 解析失败，使用默认值")
                return None
        return None

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
        except Exception as e:
            return self._error_response(
                400, "invalid_json", f"读取请求体失败: {e}", request_id
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
        except json.JSONDecodeError as e:
            return self._error_response(
                400, "invalid_json", f"JSON 解析失败: {e}", request_id
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

        # 6. 查找 endpoint
        endpoint = self._registry.get_by_path(endpoint_path)
        if not endpoint:
            logger.warning(
                f"[WebhookNotifier] request_id={request_id} endpoint 未找到: {endpoint_path}"
            )
            return self._error_response(
                404, "not_found", f"endpoint 未找到", request_id
            )

        # 7. 校验 endpoint 状态
        if not self._registry.is_endpoint_active(endpoint.name, endpoint.owner_user_id):
            logger.warning(
                f"[WebhookNotifier] request_id={request_id} "
                f"endpoint {endpoint.name} 状态不可用: {endpoint.status}"
            )
            if endpoint.status == "revoked":
                return self._error_response(
                    403, "endpoint_revoked", "endpoint 已撤销", request_id
                )
            return self._error_response(
                403,
                "endpoint_disabled",
                f"endpoint 状态: {endpoint.status}",
                request_id,
            )

        # 8. 校验 Authorization
        auth_header = request.headers.get("Authorization", "")
        if not auth_header:
            return self._error_response(
                401, "missing_authorization", "缺少 Authorization 请求头", request_id
            )

        if not auth_header.startswith("Bearer "):
            return self._error_response(
                401,
                "invalid_token",
                "Authorization 格式必须为 Bearer <token>",
                request_id,
            )

        token = auth_header[7:].strip()
        if not verify_token(
            self._registry.server_secret,
            token,
            endpoint.token_hash,
            endpoint.token_hash_algorithm,
        ):
            logger.warning(
                f"[WebhookNotifier] request_id={request_id} "
                f"endpoint={endpoint.name} token 校验失败"
            )
            return self._error_response(
                401, "invalid_token", "Bearer Token 不匹配", request_id
            )

        # 9. 识别 OMP 事件
        headers_dict = dict(request.headers)
        is_valid, err_msg = is_omp_session_stop(headers_dict, body)
        if not is_valid:
            logger.warning(
                f"[WebhookNotifier] request_id={request_id} "
                f"endpoint={endpoint.name} OMP 事件识别失败: {err_msg}"
            )
            if "不支持" in err_msg:
                return self._error_response(
                    400, "unsupported_event", err_msg, request_id
                )
            return self._error_response(400, "invalid_payload", err_msg, request_id)

        # 10. 标准化事件
        request_time = datetime.now(timezone.utc).isoformat()
        event = normalize_omp_payload(body, request_time)
        logger.info(
            f"[WebhookNotifier] request_id={request_id} "
            f"endpoint={endpoint.name} event={event.event}"
        )

        # 11. 解析 payload 中的 target alias
        payload_target_alias = body.get("target_alias") or body.get("target")

        # 12. 确定渲染模式（全局配置）
        render_mode = self._get_render_mode()
        fallback_to_text = self._get_fallback_to_text()

        # 13. 按模式分支
        if render_mode == "html_image":
            return await self._handle_html_image(
                event,
                endpoint,
                payload_target_alias,
                request_id,
                fallback_to_text,
            )
        else:
            return await self._handle_text(
                event,
                endpoint,
                payload_target_alias,
                request_id,
            )

    async def _handle_text(
        self,
        event: NormalizedEvent,
        endpoint: EndpointRecord,
        target_alias: str | None,
        request_id: str,
    ) -> web.Response:
        """纯文本渲染与发送。"""
        try:
            rendered = render_text_default(event)
        except Exception as e:
            logger.error(f"[WebhookNotifier] request_id={request_id} 文本渲染失败: {e}")
            return self._error_response(
                500, "render_failed", f"渲染失败: {e}", request_id
            )

        send_results = await self._sender.send_text(rendered, endpoint, target_alias)
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
    ) -> web.Response:
        """HTML 卡片渲染与发送。

        分阶段处理：HTML 渲染 → html_render 截图 → 图片校验 → 发送。
        任一阶段失败时按 fallback_to_text 降级或返回 500。
        """
        # ── Phase 1: HTML 模板渲染 ────────────────────────────
        try:
            html_str = render_html_default(event)
        except Exception as e:
            logger.error(
                f"[WebhookNotifier] request_id={request_id} HTML 模板渲染失败: {e}"
            )
            if fallback_to_text:
                return await self._fallback_to_text(
                    event,
                    endpoint,
                    target_alias,
                    request_id,
                    "template_render_failed",
                )
            return self._error_response(
                500, "render_failed", f"HTML 模板渲染失败: {e}", request_id
            )

        # ── Phase 2: AstrBot html_render 截图 ─────────────────
        if not self._html_render:
            logger.error(
                f"[WebhookNotifier] request_id={request_id} html_render 回调未设置"
            )
            if fallback_to_text:
                return await self._fallback_to_text(
                    event,
                    endpoint,
                    target_alias,
                    request_id,
                    "html_render_not_available",
                )
            return self._error_response(
                500, "render_failed", "html_render 回调未设置", request_id
            )

        render_options = self._get_render_options()
        # 构造模板数据 — render_html_default 使用 Jinja2 已生成 HTML，
        # 传给 html_render 时 data 可不传额外变量，但保留 event 用于兼容
        event_dict = event.to_dict()
        event_dict["generated_at"] = datetime.now(timezone.utc).isoformat()
        event_dict["event_time"] = event_dict.get("emitted_at", "")

        try:
            image_result = await self._html_render(
                html_str,
                {"event": event_dict},
                return_url=True,
                options=render_options,
            )
        except Exception as e:
            logger.error(
                f"[WebhookNotifier] request_id={request_id} html_render 截图失败: {e}"
            )
            if fallback_to_text:
                return await self._fallback_to_text(
                    event,
                    endpoint,
                    target_alias,
                    request_id,
                    "html_render_failed",
                )
            return self._error_response(
                500, "render_failed", f"html_render 截图失败: {e}", request_id
            )

        # ── Phase 3: 图片结果校验 ─────────────────────────────
        try:
            validate_image_result(image_result)
        except (ValueError, TypeError) as e:
            logger.error(
                f"[WebhookNotifier] request_id={request_id} 图片结果校验失败: {e}"
            )
            if fallback_to_text:
                return await self._fallback_to_text(
                    event,
                    endpoint,
                    target_alias,
                    request_id,
                    "image_validation_failed",
                )
            return self._error_response(
                500, "render_failed", f"图片校验失败: {e}", request_id
            )

        # ── Phase 4: 发送图片 ─────────────────────────────────
        try:
            send_results = await self._sender.send_image(
                image_result, endpoint, target_alias
            )
        except Exception as e:
            logger.error(f"[WebhookNotifier] request_id={request_id} 发送图片失败: {e}")
            if fallback_to_text:
                return await self._fallback_to_text(
                    event,
                    endpoint,
                    target_alias,
                    request_id,
                    "send_image_failed",
                )
            return self._error_response(
                500, "render_failed", f"发送图片失败: {e}", request_id
            )

        # Phase 4b: 检查是否为图片构造失败（message_build 阶段）
        # 如果全部目标都因 unsupported_image_result 失败，属于构造失败而非真实发送失败，
        # 应触发降级而非按正常发送失败返回。
        if send_results and all(
            not r.get("ok", False) and r.get("error") == "unsupported_image_result"
            for r in send_results
        ):
            logger.error(
                f"[WebhookNotifier] request_id={request_id} "
                f"图片构造失败（全部目标均为 unsupported_image_result），触发降级"
            )
            if fallback_to_text:
                return await self._fallback_to_text(
                    event,
                    endpoint,
                    target_alias,
                    request_id,
                    "image_construction_failed",
                )
            return self._error_response(
                500, "render_failed", "图片构造失败（不支持的图片结果类型）", request_id
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

    async def _fallback_to_text(
        self,
        event: NormalizedEvent,
        endpoint: EndpointRecord,
        target_alias: str | None,
        request_id: str,
        fallback_reason: str,
    ) -> web.Response:
        """html_image 失败后降级为纯文本。

        记录降级摘要，渲染纯文本并发送，返回带 fallback 标记的响应。
        """
        logger.warning(
            f"[WebhookNotifier] request_id={request_id} "
            f"html_image 回退到 text: {fallback_reason}"
        )

        try:
            rendered = render_text_default(event)
        except Exception as e:
            logger.error(
                f"[WebhookNotifier] request_id={request_id} 降级文本渲染也失败: {e}"
            )
            return self._error_response(
                500, "render_failed", f"降级文本渲染失败: {e}", request_id
            )

        send_results = await self._sender.send_text(rendered, endpoint, target_alias)

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
    ) -> web.Response:
        """构造统一渲染响应 JSON。"""
        delivered = all(r.get("ok", False) for r in send_results)
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
        }

        if not delivered:
            data["send_results"] = send_results

        message = "ok" if delivered else "partial_failure"
        return web.json_response({"code": 0, "message": message, "data": data})

    @staticmethod
    def _error_response(
        http_status: int,
        error_code: str,
        message: str,
        request_id: str,
    ) -> web.Response:
        return web.json_response(
            {
                "code": 1,
                "message": message,
                "data": {
                    "request_id": request_id,
                    "error": error_code,
                },
            },
            status=http_status,
        )
