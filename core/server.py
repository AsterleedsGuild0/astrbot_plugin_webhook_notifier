from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from astrbot.api import logger

from .models import EndpointRecord, ServerConfig
from .omp import is_omp_session_stop, normalize_omp_payload
from .registry import EndpointRegistry
from .renderer import render_text_default
from .security import verify_token
from .sender import Sender


class WebhookServer:
    """Webhook HTTP Server。

    使用 aiohttp.web，提供 POST /webhook/{endpoint} 端点。
    """

    def __init__(
        self,
        config: ServerConfig,
        registry: EndpointRegistry,
        sender: Sender,
    ) -> None:
        self._config = config
        self._registry = registry
        self._sender = sender
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
        if not self._registry.is_endpoint_active(endpoint.name):
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

        # 12. 渲染文本
        try:
            rendered = render_text_default(event)
        except Exception as e:
            logger.error(f"[WebhookNotifier] request_id={request_id} 渲染失败: {e}")
            return self._error_response(
                500, "render_failed", f"渲染失败: {e}", request_id
            )

        # 13. 发送
        send_results = await self._sender.send_text(
            rendered, endpoint, payload_target_alias
        )

        # 14. 构造响应
        delivered = all(r.get("ok", False) for r in send_results)
        target_names = [r.get("name", "unknown") for r in send_results]

        if delivered:
            return web.json_response(
                {
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "request_id": request_id,
                        "provider": "omp",
                        "event": "omp.session_stop",
                        "delivered": True,
                        "targets": target_names,
                        "render_mode": "text",
                    },
                }
            )
        else:
            # 部分失败
            return web.json_response(
                {
                    "code": 0,
                    "message": "partial_failure",
                    "data": {
                        "request_id": request_id,
                        "provider": "omp",
                        "event": "omp.session_stop",
                        "delivered": False,
                        "targets": target_names,
                        "send_results": send_results,
                        "render_mode": "text",
                    },
                }
            )

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
