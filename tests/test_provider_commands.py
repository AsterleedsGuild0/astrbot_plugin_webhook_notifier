"""单元测试 main.py 的 provider 命令辅助函数。

不依赖 AstrBot 运行时，直接测试静态/实例方法逻辑。
"""

# ruff: noqa: E402

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot.api.star import Context

from core.omp import OmpProviderAdapter
from core.providers import ProviderRegistry
from astrbot_plugin_webhook_notifier.main import WebhookNotifierPlugin


# ─── _extract_provider_flag ──────────────────────────────────


class TestExtractProviderFlag:
    """覆盖：无 flag、正常 omp/opencode、flag 末尾缺值、连续 flag、重复 flag、未知值。"""

    @staticmethod
    def extract(args: list[str]) -> tuple[str, list[str]]:
        return WebhookNotifierPlugin._extract_provider_flag(args)  # type: ignore

    def test_no_flag(self):
        """无 --provider 时返回空 provider。"""
        provider, remaining = self.extract(["private", "my_ep"])
        assert provider == ""
        assert remaining == ["private", "my_ep"]

    def test_flag_omp(self):
        """--provider omp 应正确提取。"""
        provider, remaining = self.extract(["private", "--provider", "omp"])
        assert provider == "omp"
        assert remaining == ["private"]

    def test_flag_opencode(self):
        """--provider opencode 应正确提取。"""
        provider, remaining = self.extract(["private", "--provider", "opencode"])
        assert provider == "opencode"
        assert remaining == ["private"]

    def test_flag_missing_value(self):
        """--provider 末尾缺值应返回空 provider。"""
        provider, remaining = self.extract(["private", "--provider"])
        assert provider == ""
        assert remaining == ["private"]

    def test_consecutive_flags(self):
        """连续 flag（--provider 后跟另一个 --flag）应正确解析。"""
        provider, remaining = self.extract(["private", "--provider", "omp", "--other"])
        assert provider == "omp"
        assert remaining == ["private", "--other"]

    def test_duplicate_flag(self):
        """重复 --provider 返回空 provider（由调用方拒绝）。"""
        provider, remaining = self.extract(
            ["private", "--provider", "omp", "--provider", "opencode"]
        )
        assert provider == ""  # 重复 flag 被拒绝
        # remaining 包含从第二个 --provider 开始的内容
        assert len(remaining) >= 2

    def test_duplicate_flag_same_value(self):
        """即使两次相同值，重复 --provider 也被拒绝。"""
        provider, remaining = self.extract(
            ["private", "--provider", "omp", "--provider", "omp"]
        )
        assert provider == ""

    def test_flag_in_middle_of_positional_args(self):
        """--provider 在位置参数中间也应正确提取。"""
        provider, remaining = self.extract(
            ["group", "12345", "--provider", "omp", "my_ep"]
        )
        assert provider == "omp"
        assert remaining == ["group", "12345", "my_ep"]

    def test_flag_value_is_unknown(self):
        """未知 provider 值也能提取，由 validate 阶段拒绝。"""
        provider, remaining = self.extract(["private", "--provider", "unknown"])
        assert provider == "unknown"
        assert remaining == ["private"]


# ─── _validate_create_provider ───────────────────────────────


class TestValidateCreateProvider:
    """覆盖 omp（已注册/未注册）、opencode（已注册/未注册）、未知值、registry None。"""

    @staticmethod
    def _plugin_with_registry(
        providers: list[str] | None = None,
    ) -> WebhookNotifierPlugin:
        plugin = WebhookNotifierPlugin(Context(), {})  # type: ignore[arg-type]
        if providers is None:
            # 模拟 ProviderRegistry 未初始化
            plugin._provider_registry = None
        else:
            reg = ProviderRegistry()
            for p in providers:
                if p == "omp":
                    reg.register(OmpProviderAdapter())
                else:
                    from core.providers import ProviderAdapter
                    from core.models import NormalizedEvent

                    class FakeAdapter(ProviderAdapter):
                        @property
                        def provider(self) -> str:
                            return p

                        def parse(self, **kw):
                            return NormalizedEvent(provider=p, event="test")

                    reg.register(FakeAdapter())
            reg.freeze()
            plugin._provider_registry = reg
        return plugin

    def test_omp_registered(self):
        """omp 已注册时应通过。"""
        plugin = self._plugin_with_registry(["omp"])
        ok, msg = plugin._validate_create_provider("omp")
        assert ok is True
        assert msg == ""

    def test_omp_not_registered(self):
        """omp 未注册时应拒绝。"""
        plugin = self._plugin_with_registry([])
        ok, msg = plugin._validate_create_provider("omp")
        assert ok is False
        assert "尚未注册" in msg

    def test_opencode_registered(self):
        """opencode 已注册时应通过（后续版本）。"""
        plugin = self._plugin_with_registry(["omp", "opencode"])
        ok, msg = plugin._validate_create_provider("opencode")
        assert ok is True
        assert msg == ""

    def test_opencode_not_registered(self):
        """opencode 未注册时应返回明确未启用消息。"""
        plugin = self._plugin_with_registry(["omp"])
        ok, msg = plugin._validate_create_provider("opencode")
        assert ok is False
        assert "尚未启用" in msg
        assert "opencode" in msg

    def test_unknown_provider(self):
        """未知 provider 值应返回 unsupported。"""
        plugin = self._plugin_with_registry(["omp"])
        ok, msg = plugin._validate_create_provider("custom")
        assert ok is False
        assert "不支持的 provider" in msg

    def test_registry_none(self):
        """ProviderRegistry 未初始化时应拒绝。"""
        plugin = self._plugin_with_registry(None)  # 保持 _provider_registry=None
        ok, msg = plugin._validate_create_provider("omp")
        assert ok is False
        assert "未初始化" in msg


# ─── Provider 创建后不可变 ──────────────────────────────────


class TestProviderImmutability:
    """证明 provider 创建后不可修改。"""

    def test_no_update_provider_method_in_registry(self):
        """EndpointRegistry 没有 update/change provider 方法。"""
        from core.registry import EndpointRegistry

        # 列出所有公共方法名
        public_methods = {
            name
            for name in dir(EndpointRegistry)
            if not name.startswith("_") and callable(getattr(EndpointRegistry, name))
        }
        # 检查没有 update_provider / change_provider / set_provider 方法
        update_like = {
            m
            for m in public_methods
            if "provider" in m.lower() and "create" not in m.lower()
        }
        assert update_like == set(), f"发现可能修改 provider 的方法: {update_like}"

    def test_create_sets_provider_immutably(self, tmp_path):
        """创建时设置的 provider 在记录生命周期内不变。"""
        from core.registry import EndpointRegistry

        reg = EndpointRegistry(tmp_path)
        record, _ = reg.create_private_endpoint(
            owner_platform_id="test",
            owner_user_id="user",
            name="immutable_test",
            target_umo="test:Message:1",
            provider="omp",
        )
        assert record.provider == "omp"

        # 撤销后 provider 不变
        reg.revoke_endpoint("test", "user", "immutable_test")
        revoked = reg.get_by_owner_name("test", "user", "immutable_test")
        assert revoked is not None
        assert revoked.provider == "omp"

        # 轮换 token 后 provider 不变
        record2, _ = reg.create_private_endpoint(
            owner_platform_id="test",
            owner_user_id="user2",
            name="rotate_test",
            target_umo="test:Message:1",
            provider="opencode",
        )
        assert record2.provider == "opencode"
        reg.rotate_token("test", "user2", "rotate_test")
        rotated = reg.get_by_owner_name("test", "user2", "rotate_test")
        assert rotated is not None
        assert rotated.provider == "opencode"
