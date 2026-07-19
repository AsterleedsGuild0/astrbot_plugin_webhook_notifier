"""Security module tests - no AstrBot dependency."""

from __future__ import annotations

import uuid

from core.security import (
    TOKEN_PREFIX,
    constant_time_compare,
    generate_request_id,
    generate_server_secret,
    generate_token,
    generate_verification_code,
    hash_token,
    verify_token,
)


class TestTokenGeneration:
    def test_generate_token_format(self):
        """token 格式必须为 whn_ + 32 字节 URL-safe base64。"""
        token = generate_token()
        assert token.startswith(TOKEN_PREFIX)
        # 去掉前缀后是 base64 编码的 32 字节 => 43 字符 (padding)
        payload = token[len(TOKEN_PREFIX) :]
        assert len(payload) == 43  # token_urlsafe(32) 产生 43 字符

    def test_generate_token_uniqueness(self):
        """连续生成的 token 必须不同。"""
        tokens = {generate_token() for _ in range(100)}
        assert len(tokens) == 100

    def test_generate_verification_code_format(self):
        """验证码必须为 6 位小写十六进制。"""
        code = generate_verification_code()
        assert len(code) == 6
        assert all(c in "0123456789abcdef" for c in code)

    def test_generate_request_id_uuid4(self):
        """request_id 必须是 UUID4 字符串。"""
        request_id = generate_request_id()
        parsed = uuid.UUID(request_id)
        assert str(parsed) == request_id
        assert parsed.version == 4


class TestServerSecret:
    def test_generate_server_secret(self):
        """server_secret 应为 128 字符 hex 字符串（64 字节）。"""
        secret = generate_server_secret()
        assert len(secret) == 128
        assert all(c in "0123456789abcdef" for c in secret)

    def test_multiple_secrets_differ(self):
        """多次生成的 server_secret 不同。"""
        secrets = {generate_server_secret() for _ in range(10)}
        assert len(secrets) == 10


class TestTokenHash:
    def test_hash_hmac_sha256_deterministic(self):
        """相同 server_secret + token 应产生相同哈希。"""
        secret = generate_server_secret()
        token = generate_token()
        h1 = hash_token(secret, token)
        h2 = hash_token(secret, token)
        assert h1 == h2

    def test_hash_different_secret(self):
        """不同 server_secret 产生不同哈希。"""
        secret1 = generate_server_secret()
        secret2 = generate_server_secret()
        token = generate_token()
        h1 = hash_token(secret1, token)
        h2 = hash_token(secret2, token)
        assert h1 != h2

    def test_hash_different_token(self):
        """不同 token 产生不同哈希。"""
        secret = generate_server_secret()
        t1 = generate_token()
        t2 = generate_token()
        h1 = hash_token(secret, t1)
        h2 = hash_token(secret, t2)
        assert h1 != h2

    def test_hash_is_hex(self):
        """哈希应为 64 字符 hex 字符串（SHA256）。"""
        secret = generate_server_secret()
        token = generate_token()
        h = hash_token(secret, token)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


class TestVerifyToken:
    def test_verify_correct_token(self):
        """正确 token 应验证通过。"""
        secret = generate_server_secret()
        token = generate_token()
        h = hash_token(secret, token)
        assert verify_token(secret, token, h) is True

    def test_verify_wrong_token(self):
        """错误 token 应验证失败。"""
        secret = generate_server_secret()
        token = generate_token()
        h = hash_token(secret, token)
        wrong_token = generate_token()
        assert verify_token(secret, wrong_token, h) is False

    def test_verify_wrong_secret(self):
        """错误 server_secret 应验证失败。"""
        secret1 = generate_server_secret()
        secret2 = generate_server_secret()
        token = generate_token()
        h = hash_token(secret1, token)
        assert verify_token(secret2, token, h) is False

    def test_verify_none_algorithm(self):
        """algorithm 为 None 时使用默认 hmac-sha256。"""
        secret = generate_server_secret()
        token = generate_token()
        h = hash_token(secret, token)
        assert verify_token(secret, token, h, algorithm=None) is True

    def test_verify_unknown_algorithm(self):
        """未知 algorithm 应返回 False。"""
        secret = generate_server_secret()
        token = generate_token()
        h = hash_token(secret, token)
        assert verify_token(secret, token, h, algorithm="unknown") is False


class TestConstantTimeCompare:
    def test_equal_strings(self):
        """相等字符串应返回 True。"""
        assert constant_time_compare("hello", "hello") is True

    def test_different_strings(self):
        """不同字符串应返回 False。"""
        assert constant_time_compare("hello", "world") is False

    def test_different_length(self):
        """不同长度字符串应返回 False。"""
        assert constant_time_compare("short", "longer_string") is False

    def test_empty_strings(self):
        """空字符串应相等。"""
        assert constant_time_compare("", "") is True

    def test_same_hash_values(self):
        """相同 HMAC 哈希应相等。"""
        secret = generate_server_secret()
        token = generate_token()
        h1 = hash_token(secret, token)
        h2 = hash_token(secret, token)
        assert constant_time_compare(h1, h2) is True
