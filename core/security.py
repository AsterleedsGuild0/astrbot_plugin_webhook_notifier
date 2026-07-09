from __future__ import annotations

import hmac
import os
import secrets
import uuid
from hashlib import sha256
from pathlib import Path

from astrbot.api import logger

SERVER_SECRET_FILENAME = "server_secret"
TOKEN_PREFIX = "whn_"
TOKEN_HASH_ALGORITHM = "hmac-sha256"


def generate_server_secret() -> str:
    """生成 64 字节 server_secret，返回 hex 字符串。"""
    return secrets.token_hex(64)


def load_server_secret(data_dir: str | Path) -> str:
    """从插件数据目录加载 server_secret，不存在则生成并保存。"""
    path = Path(data_dir) / SERVER_SECRET_FILENAME
    if path.exists():
        secret = path.read_text(encoding="utf-8").strip()
        if secret:
            return secret
        logger.warning("[WebhookNotifier] server_secret 文件为空，将重新生成")
    secret = generate_server_secret()
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    path.write_text(secret, encoding="utf-8")
    logger.info("[WebhookNotifier] 已生成新的 server_secret")
    return secret


def generate_token() -> str:
    """生成 Webhook token 明文。

    格式: whn_<32 字节随机值的 URL-safe base64>
    """
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(server_secret: str, token: str) -> str:
    """使用 HMAC-SHA256 计算 token 哈希。

    Args:
        server_secret: 服务端密钥。
        token: token 明文。

    Returns:
        hex 格式的 HMAC-SHA256 摘要。
    """
    return hmac.new(
        server_secret.encode("utf-8"),
        token.encode("utf-8"),
        sha256,
    ).hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    """恒定时间比较两个字符串。

    使用 hmac.compare_digest 实现，防止时序攻击。
    """
    return hmac.compare_digest(a, b)


def verify_token(
    server_secret: str, token: str, stored_hash: str, algorithm: str | None = None
) -> bool:
    """校验 token 是否匹配存储的哈希。

    当前仅支持 hmac-sha256。

    Args:
        server_secret: 服务端密钥。
        token: 需要校验的 token 明文。
        stored_hash: 存储的哈希值。
        algorithm: 哈希算法标识，默认 hmac-sha256。

    Returns:
        True 匹配，False 不匹配。
    """
    if algorithm and algorithm != TOKEN_HASH_ALGORITHM:
        logger.warning(f"[WebhookNotifier] 不支持的 token hash 算法: {algorithm}")
        return False
    computed = hash_token(server_secret, token)
    return constant_time_compare(computed, stored_hash)


def generate_verification_code() -> str:
    """生成 6 位小写十六进制验证码。"""
    return secrets.token_hex(3)


def generate_request_id() -> str:
    """生成 UUID4 格式的 request_id。"""
    return str(uuid.uuid4())
