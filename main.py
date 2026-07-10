from __future__ import annotations

import json
import os
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.platform.message_type import MessageType

from .core.models import EndpointStatus, ServerConfig
from .core.registry import (
    EndpointRegistry,
    build_endpoint_path,
    normalize_endpoint_name,
)
from .core.sender import Sender
from .core.server import WebhookServer

PLUGIN_NAME = "Webhook Notifier"
COMMAND = "whn"


@register(
    "astrbot_plugin_webhook_notifier",
    "AsterleedsGuild0",
    "接收外部 Webhook 事件并推送到指定 AstrBot 会话",
    "v0.1.0",
)
class WebhookNotifierPlugin(Star):
    """AstrBot Webhook Notifier Milestone 1：文本链路 MVP。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config

        # 运行时组件（在 initialize 中初始化）
        self._registry: EndpointRegistry | None = None
        self._sender: Sender | None = None
        self._server: WebhookServer | None = None
        self._server_config: ServerConfig | None = None

    async def initialize(self) -> None:
        if not self._enabled:
            logger.info("[WebhookNotifier] 插件已禁用，跳过初始化")
            return

        # 初始化插件数据目录
        try:
            data_dir = StarTools.get_data_dir(
                plugin_name="astrbot_plugin_webhook_notifier"
            )
        except (ValueError, RuntimeError) as e:
            logger.error(
                f"[WebhookNotifier] 无法获取插件数据目录: {e}，将使用 fallback 路径"
            )
            data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(data_dir, exist_ok=True)
        logger.info(f"[WebhookNotifier] 插件数据目录: {data_dir}")

        # 初始化 Registry
        self._registry = EndpointRegistry(data_dir)
        self._registry.expire_stale_pending()

        # 初始化 Sender
        self._sender = Sender(self.context)

        # 初始化服务器配置
        self._server_config = ServerConfig.from_plugin_config(
            self.config  # type: ignore[arg-type]
        )

        # 如果有任何 active endpoint，启动 HTTP 服务
        if self._registry.count_active() > 0:
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
                "请使用 /whn token new 命令创建 managed endpoint。"
            )

        logger.info("[WebhookNotifier] 插件 Milestone 1 已初始化")

    async def terminate(self) -> None:
        if self._server:
            await self._server.stop()
        logger.info("[WebhookNotifier] 插件已卸载")

    # ─── 状态命令 ───────────────────────────────────────────

    @filter.command("webhook_notifier")
    async def status_long(self, event: AstrMessageEvent):
        """查看 Webhook Notifier 完整状态。"""
        yield self._plain_text_result(event, self._build_status_text())

    @filter.command("whn")
    async def status_short(self, event: AstrMessageEvent):
        """Webhook Notifier 短命令入口。"""
        args = self._normalize_whn_args(event.message_str)
        if not args:
            yield self._plain_text_result(event, self._build_status_text())
            return

        result = await self._dispatch_whn_command(event, args)
        if result:
            yield self._plain_text_result(event, result)

    def _plain_text_result(self, event: AstrMessageEvent, text: str):
        """构造强制纯文本的命令响应，避免 URL/Token 被 AstrBot T2I 转成图片。"""
        return event.plain_result(text).use_t2i(False)

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
        self, event: AstrMessageEvent, args: str
    ) -> str | None:
        """分发 /whn 子命令。"""
        parts = args.split()
        if not parts:
            return self._build_status_text()

        sub = parts[0].lower()

        if sub in ("status", "状态"):
            return self._build_status_text()
        if sub == "token":
            return await self._handle_token_command(event, parts[1:])
        else:
            return (
                f"未知子命令: {sub}\n"
                f"可用命令：\n"
                f"  /whn                   查看状态\n"
                f"  /whn status            查看状态\n"
                f"  /whn token new private [名称]  创建私聊 endpoint\n"
                f"  /whn token new group <群号> [名称]  创建群聊 endpoint\n"
                f"  /whn token verify <request_id> <code>  验证群聊 endpoint\n"
                f"  /whn token list        列出自己的 endpoint\n"
                f"  /whn token rotate <名称>  轮换 token\n"
                f"  /whn token revoke <名称>  撤销 endpoint"
            )

    async def _handle_token_command(
        self, event: AstrMessageEvent, args: list[str]
    ) -> str:
        """处理 /whn token 子命令。"""
        if not args:
            return (
                "用法:\n"
                "  /whn token new private [名称]\n"
                "  /whn token new group <群号> [名称]\n"
                "  /whn token verify <request_id> <code>\n"
                "  /whn token list\n"
                "  /whn token rotate <名称>\n"
                "  /whn token revoke <名称>"
            )

        action = args[0].lower()

        if action == "new":
            return await self._handle_token_new(event, args[1:])
        elif action == "verify":
            return await self._handle_token_verify(event, args[1:])
        elif action == "list":
            return self._handle_token_list(event)
        elif action == "rotate":
            return await self._handle_token_rotate(event, args[1:])
        elif action == "revoke":
            return await self._handle_token_revoke(event, args[1:])
        else:
            return f"未知 token 子命令: {action}"

    # ─── Token 管理 ──────────────────────────────────────────

    async def _handle_token_new(self, event: AstrMessageEvent, args: list[str]) -> str:
        """处理 /whn token new 命令。"""
        # 必须是私聊
        if not self._is_private(event):
            return "❌ 创建 endpoint 必须在私聊中执行。"

        if not args:
            return (
                "用法:\n"
                "  /whn token new private [名称]\n"
                "  /whn token new group <群号> [名称]"
            )

        mode = args[0].lower()
        name_param = " ".join(args[1:]).strip() if len(args) > 1 else ""

        if mode == "private":
            return await self._create_private_endpoint(event, name_param)
        elif mode == "group":
            return await self._create_group_pending(event, args[1:])
        else:
            return (
                f"未知类型: {mode}\n"
                "用法: /whn token new private [名称] 或 /whn token new group <群号> [名称]"
            )

    async def _create_private_endpoint(
        self, event: AstrMessageEvent, name_param: str
    ) -> str:
        """创建私聊 endpoint。"""
        registry = self._ensure_registry()
        if not registry:
            return "❌ Registry 未初始化，请检查日志。"

        owner_id = event.get_sender_id()
        target_umo = event.unified_msg_origin

        endpoint_name = normalize_endpoint_name(name_param or "private")
        endpoint_path = build_endpoint_path(owner_id, endpoint_name)

        # 检查是否已存在
        if registry.get_by_owner_name(owner_id, endpoint_name):
            return (
                f"❌ 你已经创建过名为 '{endpoint_name}' 的 endpoint，请使用其他名称。"
            )
        if registry.get_by_path(endpoint_path):
            return f"❌ endpoint 路径 '{endpoint_path}' 已存在，请使用其他名称。"

        try:
            record, token = registry.create_private_endpoint(
                name=endpoint_name,
                path=endpoint_path,
                owner_user_id=owner_id,
                target_umo=target_umo,
                render_mode=self._get_render_mode(),
                description=f"私聊 endpoint for {owner_id}",
            )
        except Exception as e:
            logger.error(f"[WebhookNotifier] 创建私聊 endpoint 失败: {e}")
            return f"❌ 创建失败: {e}"

        # 如果 HTTP 服务未运行，启动
        await self._ensure_server_running()

        # 构造返回信息
        url = self._build_webhook_url(endpoint_path)
        return (
            f"✅ 私聊 endpoint 创建成功\n\n"
            f"名称: {endpoint_name}\n"
            f"URL: {url}\n"
            f"Bearer Token: {token}\n\n"
            f"OMP 环境变量配置:\n"
            f"  OMP_SESSION_WEBHOOK_URL={url}\n"
            f"  OMP_SESSION_WEBHOOK_TOKEN={token}\n\n"
            f"⚠️ 安全提示：Token 只展示一次，泄露后请立即使用 /whn token rotate {endpoint_name} 轮换。"
        )

    async def _create_group_pending(
        self, event: AstrMessageEvent, args: list[str]
    ) -> str:
        """创建群聊待验证 endpoint。"""
        registry = self._ensure_registry()
        if not registry:
            return "❌ Registry 未初始化，请检查日志。"

        if not args:
            return "❌ 请指定 QQ 群号: /whn token new group <群号> [名称]"

        group_id = args[0].strip()
        # 简单校验群号格式
        if not group_id.isdigit():
            return "❌ 群号必须是数字。"

        name_param = " ".join(args[1:]).strip() if len(args) > 1 else ""
        owner_id = event.get_sender_id()

        endpoint_name = normalize_endpoint_name(name_param or f"group_{group_id}")
        endpoint_path = build_endpoint_path(owner_id, endpoint_name)

        if registry.get_by_owner_name(owner_id, endpoint_name):
            return (
                f"❌ 你已经创建过名为 '{endpoint_name}' 的 endpoint，请使用其他名称。"
            )
        if registry.get_by_path(endpoint_path):
            return f"❌ endpoint 路径 '{endpoint_path}' 已存在，请使用其他名称。"

        try:
            record, request_id, code = registry.create_pending_verification(
                name=endpoint_name,
                path=endpoint_path,
                owner_user_id=owner_id,
                target_group_id=group_id,
                render_mode=self._get_render_mode(),
                description=f"群聊 endpoint for {owner_id} 目标群 {group_id}",
            )
        except Exception as e:
            logger.error(f"[WebhookNotifier] 创建群聊待验证 endpoint 失败: {e}")
            return f"❌ 创建失败: {e}"

        return (
            f"✅ 待验证申请已创建\n\n"
            f"名称: {endpoint_name}\n"
            f"目标群: {group_id}\n"
            f"请求 ID: {request_id}\n"
            f"验证码: {code}\n"
            f"有效期: 10 分钟\n\n"
            f"请在目标群中发送以下命令完成验证：\n"
            f"  /whn token verify {request_id} {code}\n\n"
            f"验证通过后，Token 将通过私聊发送给您。"
        )

    async def _handle_token_verify(
        self, event: AstrMessageEvent, args: list[str]
    ) -> str:
        """处理 /whn token verify 命令。"""
        registry = self._ensure_registry()
        if not registry:
            return "❌ Registry 未初始化，请检查日志。"

        # 必须在群聊中执行
        if not self._is_group(event):
            return "❌ 验证命令必须在目标群中执行。"

        if len(args) < 2:
            return "❌ 用法: /whn token verify <request_id> <code>"

        request_id = args[0].strip()
        code = args[1].strip()
        verify_user_id = event.get_sender_id()
        verify_group_id = event.get_group_id()

        if not verify_group_id:
            return "❌ 无法获取当前群 ID，请确认 Bot 已在群内。"

        # 检查群管理员权限
        is_admin = await self._check_group_admin(event)
        if is_admin is None:
            return "❌ 当前平台无法校验群管理员身份，群聊 token 验证失败。"
        if not is_admin:
            return (
                "❌ 权限不足：群聊 token 验证需要执行者是群主或群管理员。\n"
                "如需创建群聊 endpoint，请联系群主或管理员操作。"
            )

        # 构造群聊 UMO
        platform_id = event.get_platform_id()
        group_umo = f"{platform_id}:GroupMessage:{verify_group_id}"

        pending_info = dict(registry._pending.get(request_id, {}))
        result_status, result_msg, token = registry.verify_group_endpoint(
            request_id=request_id,
            code=code,
            verify_user_id=verify_user_id,
            verify_group_id=str(verify_group_id),
            group_target_umo=group_umo,
        )

        if result_status == "ok" and token:
            # 验证成功：私聊返回 token
            await self._ensure_server_running()

            # 获取 endpoint 信息。verify_group_endpoint 会清理 pending，
            # 因此这里使用验证前复制出的 pending_info。
            endpoint_name = pending_info.get("endpoint_name", "unknown")
            owner_user_id = pending_info.get("owner_user_id", verify_user_id)
            record = registry.get_by_owner_name(owner_user_id, endpoint_name)
            endpoint_path = record.path if record else endpoint_name
            url = self._build_webhook_url(endpoint_path)

            # 仅在私聊返回 token。verify 命令发生在群聊事件中，不能使用
            # event.unified_msg_origin，否则 token 明文会被发送到群内。
            private_umo = f"{platform_id}:FriendMessage:{verify_user_id}"
            private_msg = (
                f"✅ 群聊 endpoint 验证成功！\n\n"
                f"名称: {endpoint_name}\n"
                f"URL: {url}\n"
                f"Bearer Token: {token}\n\n"
                f"OMP 环境变量配置:\n"
                f"  OMP_SESSION_WEBHOOK_URL={url}\n"
                f"  OMP_SESSION_WEBHOOK_TOKEN={token}\n\n"
                f"⚠️ 安全提示：Token 只展示一次，泄露后请立即使用 /whn token rotate {endpoint_name} 轮换。"
            )
            # 尝试私聊发送 token
            try:
                sent = await self.context.send_message(
                    private_umo, MessageChain([Plain(private_msg)]).use_t2i(False)
                )
                if not sent:
                    logger.error(
                        f"[WebhookNotifier] 私聊发送 token 失败: 找不到平台会话 {private_umo}"
                    )
                    return (
                        "❌ 验证成功但私聊发送 Token 失败：无法找到私聊会话。"
                        "请先私聊 Bot 任意消息后，再使用 /whn token rotate "
                        f"{endpoint_name} 重新生成 Token。"
                    )
                logger.info(
                    f"[WebhookNotifier] 群聊验证成功，Token 已私聊发送给用户 {verify_user_id}"
                )
            except Exception as e:
                logger.error(f"[WebhookNotifier] 私聊发送 token 失败: {e}")
                return f"❌ 验证成功但私聊发送 Token 失败: {e}"

            return "✅ 验证成功！Token 已通过私聊发送给您，请查看私聊消息。"
        else:
            return f"❌ {result_msg}"

    def _handle_token_list(self, event: AstrMessageEvent) -> str:
        """列出用户的所有 endpoint。"""
        registry = self._ensure_registry()
        if not registry:
            return "❌ Registry 未初始化，请检查日志。"

        # 必须是私聊
        if not self._is_private(event):
            return "❌ 查看 endpoint 列表请在私聊中执行。"

        owner_id = event.get_sender_id()
        records = registry.list_visible_by_owner(owner_id)

        if not records:
            return (
                "📋 您当前没有可用的 endpoint。\n"
                "已撤销或已过期的 endpoint 不在默认列表中显示。\n"
                "使用 /whn token new private [名称] 创建。"
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
                f"   路径: /{rec.path}\n"
                f"   状态: {rec.status}\n"
                f"   渲染: {rec.render_mode}\n"
                f"   目标: {targets_str}\n"
                f"   创建: {rec.created_at[:19]}\n"
            )
            if rec.revoked_at:
                lines.append(f"   撤销: {rec.revoked_at[:19]}\n")

        return "\n".join(lines).strip()

    async def _handle_token_rotate(
        self, event: AstrMessageEvent, args: list[str]
    ) -> str:
        """轮换 token。"""
        registry = self._ensure_registry()
        if not registry:
            return "❌ Registry 未初始化，请检查日志。"

        if not self._is_private(event):
            return "❌ 轮换 token 请在私聊中执行。"

        if not args:
            return "❌ 请指定 endpoint 名称: /whn token rotate <名称>"

        name = normalize_endpoint_name(" ".join(args))
        owner_id = event.get_sender_id()

        success, result = registry.rotate_token(name, owner_id)
        if not success:
            return f"❌ {result}"

        record = registry.get_by_owner_name(owner_id, name)
        endpoint_path = record.path if record else name
        url = self._build_webhook_url(endpoint_path)

        return (
            f"✅ Token 已轮换\n\n"
            f"名称: {name}\n"
            f"新的 Bearer Token: {result}\n"
            f"URL: {url}\n\n"
            f"⚠️ 旧 Token 已立即失效。请更新外部系统配置。"
        )

    async def _handle_token_revoke(
        self, event: AstrMessageEvent, args: list[str]
    ) -> str:
        """撤销 endpoint。"""
        registry = self._ensure_registry()
        if not registry:
            return "❌ Registry 未初始化，请检查日志。"

        if not self._is_private(event):
            return "❌ 撤销 endpoint 请在私聊中执行。"

        if not args:
            return "❌ 请指定 endpoint 名称: /whn token revoke <名称>"

        name = normalize_endpoint_name(" ".join(args))
        owner_id = event.get_sender_id()

        success, message = registry.revoke_endpoint(name, owner_id)
        if not success:
            return f"❌ {message}"

        return f"✅ {message}"

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
            )
            await self._server.start()
        except Exception as e:
            logger.error(f"[WebhookNotifier] 启动 HTTP 服务失败: {e}")

    def _build_webhook_url(self, endpoint_path: str) -> str:
        """构建 Webhook URL。"""
        if self._server_config and self._server_config.public_base_url:
            base = self._server_config.public_base_url.rstrip("/")
            return f"{base}/{endpoint_path}"
        else:
            host = self._server_config.host if self._server_config else "127.0.0.1"
            port = self._server_config.port if self._server_config else 18080
            base_path = (
                self._server_config.base_path if self._server_config else "/webhook"
            )
            local_url = f"http://{host}:{port}{base_path}/{endpoint_path}"
            return (
                f"{local_url}\n\n"
                f"⚠️ 未配置 public_base_url，返回的是本地监听地址。"
                f"如需公网访问，请配置 server.public_base_url 并确保 HTTPS 反向代理。"
            )

    def _get_render_mode(self) -> str:
        mode = self.config.get("render_mode", "text")
        if mode == "html_image":
            logger.info(
                "[WebhookNotifier] render_mode 配置为 html_image，但 MS1 仅支持 text。将降级为 text。"
            )
            return "text"
        return mode

    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        """检查是否为私聊消息。"""
        return event.session.message_type == MessageType.FRIEND_MESSAGE

    @staticmethod
    def _is_group(event: AstrMessageEvent) -> bool:
        """检查是否为群聊消息。"""
        return event.session.message_type == MessageType.GROUP_MESSAGE

    async def _check_group_admin(self, event: AstrMessageEvent) -> bool | None:
        """检查当前用户在当前群是否是群主或群管理员。

        Returns:
            bool: 是管理员。
            None: 无法判断（平台不支持）。
        """
        # 先尝试 event.is_admin()
        try:
            if event.is_admin():
                return True
        except (AttributeError, TypeError, NotImplementedError):
            pass

        # 尝试通过群对象获取管理员信息
        try:
            group_info = event.get_group()
            if group_info is None:
                return None

            sender_id = event.get_sender_id()

            # 检查是否为群主
            owner_id = (
                getattr(group_info, "group_owner", None)
                or getattr(group_info, "owner", None)
                or getattr(group_info, "owner_id", None)
            )
            if owner_id and str(owner_id) == str(sender_id):
                return True

            # 检查是否为管理员
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
                        else getattr(admin, "user_id", None)
                        or getattr(admin, "id", None)
                    )
                    if admin_id and str(admin_id) == str(sender_id):
                        return True
        except (AttributeError, TypeError, NotImplementedError):
            pass

        # 无法判断
        return None

    # ─── 状态构建 ───────────────────────────────────────────

    def _build_status_text(self) -> str:
        enabled = self._enabled
        server_running = self._server is not None and self._server.running
        active_count = self._registry.count_active() if self._registry else 0
        render_mode = self._get_render_mode()
        fallback = self.config.get("fallback_to_text", True)
        templates_dir = self.config.get("templates_dir", "templates")
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
            f"渲染模式：{render_mode}（MS1 仅支持 text）",
            f"渲染降级：{'开启' if fallback else '关闭'}",
            f"模板目录：{templates_dir}",
            f"监听地址：http://{host}:{port}{base_path}",
        ]

        if self._server_config and self._server_config.public_base_url:
            lines.append(f"公网地址：{self._server_config.public_base_url}")

        if legacy_token:
            lines.append("")
            lines.append("⚠️ 检测到早期占位 webhook_token 配置（MVP 未启用）")
            lines.append("   请使用 /whn token new 命令创建 managed endpoint。")

        lines.extend(
            [
                "",
                "可用命令：",
                "  /webhook_notifier  查看完整状态",
                "  /whn               查看状态 / 管理 endpoint",
            ]
        )

        return "\n".join(lines)

    @property
    def _enabled(self) -> bool:
        return bool(self.config.get("enabled", True))
