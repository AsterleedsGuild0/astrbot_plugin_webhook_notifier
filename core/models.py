from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class EndpointStatus(str, enum.Enum):
    """Endpoint 生命周期状态。"""

    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass
class TargetAlias:
    """目标别名。"""

    name: str
    umo: str  # Unified Message Origin, e.g. aiocqhttp:GroupMessage:123456789


@dataclass
class EndpointRecord:
    """端到端注册记录。

    每条记录对应一个独立 endpoint/token 绑定关系。
    token 明文不持久化，只保存 token_hash。
    """

    name: str
    path: str
    provider: str
    token_hash: str
    token_hash_algorithm: str
    owner_user_id: str
    targets: list[TargetAlias]  # 目标白名单
    render_mode: str  # "text" | "html_image"
    template: str | None  # 自定义模板文件名，None 为默认模板
    status: str  # EndpointStatus 的值
    created_at: str  # ISO-8601 UTC
    revoked_at: str | None = None  # ISO-8601 UTC, None 表示未撤销
    pending_request_id: str | None = None  # 群聊验证的 request_id
    pending_code: str | None = None  # 群聊验证的 code
    pending_expires_at: str | None = None  # 验证码过期时间 ISO-8601
    description: str | None = None


@dataclass
class NormalizedEvent:
    """标准化事件对象，作为 renderer 和 sender 的统一输入。"""

    provider: str
    event: str
    version: int = 1
    id: str = ""
    emitted_at: str = ""  # ISO-8601 UTC
    title: str = ""
    status: str = "success"  # success | warning | failed | info | unknown
    summary: str = ""
    source: dict[str, Any] = field(default_factory=lambda: {"name": "", "url": None})
    actor: dict[str, Any] = field(default_factory=lambda: {"name": None, "url": None})
    fields: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "event": self.event,
            "version": self.version,
            "id": self.id,
            "emitted_at": self.emitted_at,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "source": self.source,
            "actor": self.actor,
            "fields": self.fields,
            "links": self.links,
            "raw": self.raw,
        }


@dataclass
class PendingVerification:
    """群聊 token 待验证申请。"""

    request_id: str  # UUID4
    code: str  # 6 位小写十六进制
    endpoint_name: str
    owner_user_id: str
    target_group_id: str  # QQ 群号
    created_at: str  # ISO-8601 UTC
    expires_at: str  # ISO-8601 UTC


@dataclass
class ServerConfig:
    """Webhook 服务器配置。"""

    host: str = "127.0.0.1"
    port: int = 18080
    base_path: str = "/webhook"
    public_base_url: str = ""
    body_limit_bytes: int = 262144  # 256 KiB

    @classmethod
    def from_plugin_config(cls, config: dict) -> ServerConfig:
        server_cfg = config.get("server", {})
        if isinstance(server_cfg, str):
            import json

            try:
                server_cfg = json.loads(server_cfg)
            except (json.JSONDecodeError, TypeError):
                server_cfg = {}
        if not isinstance(server_cfg, dict):
            server_cfg = {}
        return cls(
            host=server_cfg.get("host", "127.0.0.1"),
            port=int(server_cfg.get("port", 18080)),
            base_path=server_cfg.get("base_path", "/webhook").rstrip("/"),
            public_base_url=server_cfg.get("public_base_url", ""),
            body_limit_bytes=int(server_cfg.get("body_limit_bytes", 262144)),
        )
