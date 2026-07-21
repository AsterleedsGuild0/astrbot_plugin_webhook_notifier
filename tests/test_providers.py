"""Provider Adapter & Registry tests - no AstrBot dependency."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.models import NormalizedEvent
from core.omp import (
    OMP_SOURCE_NAME,
    OmpProviderAdapter,
    is_omp_session_stop,
    normalize_omp_payload,
)
from core.providers import (
    ProviderAdapter,
    ProviderError,
    ProviderRegistry,
    ProviderRegistryError,
)


# ─── Provider Registry ─────────────────────────────────────────


class TestProviderRegistry:
    def test_register_and_get(self):
        """显式注册 adapter 后可精确获取。"""
        reg = ProviderRegistry()
        adapter = OmpProviderAdapter()
        reg.register(adapter)
        assert reg.get("omp") is adapter

    def test_get_returns_none_for_unregistered(self):
        """未注册 provider 返回 None。"""
        reg = ProviderRegistry()
        assert reg.get("non_existent") is None

    def test_duplicate_registration_raises(self):
        """重复 provider key 在初始化时失败。"""
        reg = ProviderRegistry()
        reg.register(OmpProviderAdapter())
        with pytest.raises(ProviderRegistryError, match="重复注册"):
            reg.register(OmpProviderAdapter())

    def test_registration_order_does_not_affect_get(self):
        """注册顺序不影响 get() 结果。"""
        reg = ProviderRegistry()

        class FakeA(ProviderAdapter):
            @property
            def provider(self) -> str:
                return "fake_a"

            def parse(self, **kwargs) -> NormalizedEvent:
                return NormalizedEvent(provider="fake_a", event="test")

        class FakeB(ProviderAdapter):
            @property
            def provider(self) -> str:
                return "fake_b"

            def parse(self, **kwargs) -> NormalizedEvent:
                return NormalizedEvent(provider="fake_b", event="test")

        a = FakeA()
        b = FakeB()
        reg.register(b)
        reg.register(a)
        assert reg.get("fake_a") is a
        assert reg.get("fake_b") is b

    def test_providers_property(self):
        """providers 属性返回已注册的 provider 集合。"""
        reg = ProviderRegistry()
        assert reg.providers == frozenset()
        reg.register(OmpProviderAdapter())
        assert reg.providers == frozenset({"omp"})

    def test_empty_provider_key_raises(self):
        """空 provider key 应拒绝注册。"""
        reg = ProviderRegistry()

        class EmptyKeyAdapter(ProviderAdapter):
            @property
            def provider(self) -> str:
                return ""

            def parse(self, **kwargs) -> NormalizedEvent:
                return NormalizedEvent(provider="", event="test")

        with pytest.raises(ProviderRegistryError, match="不能为空"):
            reg.register(EmptyKeyAdapter())


# ─── ProviderRegistry Freeze ────────────────────────────────


class TestProviderRegistryFreeze:
    def test_freeze_prevents_register(self):
        """freeze 后 register 应抛出 ProviderRegistryError。"""
        reg = ProviderRegistry()
        reg.register(OmpProviderAdapter())
        reg.freeze()
        from core.providers import ProviderAdapter

        class AnotherAdapter(ProviderAdapter):
            @property
            def provider(self) -> str:
                return "another"

            def parse(self, **kwargs) -> NormalizedEvent:
                return NormalizedEvent(provider="another", event="test")

        with pytest.raises(ProviderRegistryError, match="冻结"):
            reg.register(AnotherAdapter())

    def test_freeze_is_idempotent(self):
        """重复 freeze() 不应出错。"""
        reg = ProviderRegistry()
        reg.freeze()
        reg.freeze()  # 幂等

    def test_frozen_get_still_works(self):
        """freeze 后 get() 仍正常。"""
        reg = ProviderRegistry()
        reg.register(OmpProviderAdapter())
        reg.freeze()
        assert reg.get("omp") is not None
        assert reg.get("missing") is None

    def test_is_frozen_property(self):
        """is_frozen 在 freeze 前为 False，后为 True。"""
        reg = ProviderRegistry()
        assert reg.is_frozen is False
        reg.freeze()
        assert reg.is_frozen is True

    def test_freeze_does_not_affect_other_instances(self):
        """不同实例的 freeze 互不影响。"""
        reg_a = ProviderRegistry()
        reg_b = ProviderRegistry()
        reg_a.freeze()
        assert reg_a.is_frozen is True
        assert reg_b.is_frozen is False
        reg_b.register(OmpProviderAdapter())  # 应成功


class TestProviderError:
    def test_default_retryable(self):
        err = ProviderError("test_code", "test message")
        assert err.code == "test_code"
        assert err.retryable is False
        assert str(err) == "test message"

    def test_retryable_true(self):
        err = ProviderError("unavailable", "not ready", retryable=True)
        assert err.retryable is True


# ─── OMP Provider Adapter ────────────────────────────────────


class TestOmpProviderAdapterProviderProperty:
    def test_provider_is_omp(self):
        assert OmpProviderAdapter().provider == "omp"


class TestOmpProviderAdapterParse:
    """OmpProviderAdapter.parse() 应正确处理各类合法/非法 payload。"""

    def _make_adapter(self) -> OmpProviderAdapter:
        return OmpProviderAdapter()

    def _received_at(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def test_valid_session_stop_header_only(self):
        """仅 Header 的合法请求应成功。"""
        event = self._make_adapter().parse(
            headers={"x-omp-event": "session_stop"},
            payload={},
            received_at=self._received_at(),
        )
        assert event.provider == "omp"
        assert event.event == "omp.session_stop"

    def test_valid_session_stop_body_only(self):
        """仅 Body 的合法请求应成功。"""
        event = self._make_adapter().parse(
            headers={},
            payload={"event": "omp.session_stop"},
            received_at=self._received_at(),
        )
        assert event.provider == "omp"

    def test_invalid_raises_provider_error(self):
        """无效 payload 应抛出 ProviderError(unrecognized_event)。"""
        with pytest.raises(ProviderError) as exc_info:
            self._make_adapter().parse(
                headers={}, payload={}, received_at=self._received_at()
            )
        assert exc_info.value.code == "unrecognized_event"
        assert exc_info.value.retryable is False
        # 确认不泄露底层异常原文或 payload 片段（OMP 作为 provider 名是安全的）
        assert "无法识别" in str(exc_info.value)
        assert "event" not in str(exc_info.value)

    def test_unsupported_event_raises_provider_error(self):
        """不支持的事件应抛出 ProviderError(unsupported_event)。"""
        with pytest.raises(ProviderError) as exc_info:
            self._make_adapter().parse(
                headers={},
                payload={"event": "omp.unknown"},
                received_at=self._received_at(),
            )
        assert exc_info.value.code == "unsupported_event"
        assert exc_info.value.retryable is False

    def test_header_body_conflict_raises_provider_error(self):
        """Header/Body event 冲突应抛出 ProviderError(event_mismatch)。"""
        with pytest.raises(ProviderError) as exc_info:
            self._make_adapter().parse(
                headers={"x-omp-event": "session_stop"},
                payload={"event": "omp.unknown"},
                received_at=self._received_at(),
            )
        assert exc_info.value.code == "event_mismatch"
        assert exc_info.value.retryable is False
        assert "Header" in str(exc_info.value)
        assert "Body" in str(exc_info.value)
        # 确认不泄露底层原始 event 值
        assert "session_stop" not in str(exc_info.value)
        assert "omp.unknown" not in str(exc_info.value)

    def test_full_payload_maps_fields_correctly(self):
        """完整 payload 应正确映射 NormalizedEvent 字段。"""
        body = {
            "event": "omp.session_stop",
            "emittedAt": "2026-07-08T12:00:00.000Z",
            "session": {
                "id": "sess_001",
                "name": "Test session",
                "file": "/tmp/session.json",
                "cwd": "/tmp",
                "model": "gpt-5.5",
            },
            "round": {
                "turnId": "turn_001",
                "startedAt": "2026-07-08T11:59:00.000Z",
                "endedAt": "2026-07-08T12:00:00.000Z",
                "durationMs": 57700,
                "promptLength": 977,
                "imageCount": 1,
                "messageCountDelta": 2,
            },
            "metadata": {"version": "0.1.0", "eventName": "session_stop"},
        }
        event = self._make_adapter().parse(
            headers={"x-omp-event": "session_stop"},
            payload=body,
            received_at=self._received_at(),
        )
        assert event.provider == "omp"
        assert event.event == "omp.session_stop"
        assert event.title == "会话完成"
        field_labels = {f["label"] for f in event.fields}
        assert "会话" in field_labels
        assert "模型" in field_labels

    def test_emitted_at_missing_uses_received_at(self):
        """缺少 emittedAt 时应使用 received_at。"""
        received = "2026-07-09T10:00:00.000000+00:00"
        event = self._make_adapter().parse(
            headers={},
            payload={"event": "omp.session_stop"},
            received_at=received,
        )
        assert event.emitted_at == received

    def test_adapter_does_not_touch_token_or_renderer(self):
        """验证 adapter.parse 的输入和输出边界：不访问 Token、Endpoint、renderer。"""
        adapter = self._make_adapter()
        result = adapter.parse(
            headers={"x-omp-event": "session_stop"},
            payload={"event": "omp.session_stop"},
            received_at=self._received_at(),
        )
        assert isinstance(result, NormalizedEvent)
        # adapter 不应设置 raw 外的敏感字段
        assert result.actor["name"] is None
        assert result.actor["url"] is None
        assert result.source["name"] == OMP_SOURCE_NAME


# ─── Characterization Tests ──────────────────────────────────


class TestOmpCharacterization:
    """迁移前的 characterization tests — 锁定现有行为。"""

    def test_is_omp_session_stop_valid(self):
        """锁定 is_omp_session_stop 对合法输入返回 True。"""
        valid, msg = is_omp_session_stop(
            {"x-omp-event": "session_stop"}, {"event": "omp.session_stop"}
        )
        assert valid is True
        assert msg == ""

    def test_is_omp_session_stop_empty(self):
        """锁定 is_omp_session_stop 对空输入返回 False。"""
        valid, msg = is_omp_session_stop({}, {})
        assert valid is False

    def test_is_omp_session_stop_header_body_conflict(self):
        """锁定 Header/Body 不一致行为。"""
        valid, msg = is_omp_session_stop(
            {"x-omp-event": "session_stop"}, {"event": "omp.unknown"}
        )
        assert valid is False
        assert "不一致" in msg

    def test_is_omp_session_stop_unknown_event(self):
        """锁定未知事件拒绝行为。"""
        valid, msg = is_omp_session_stop({}, {"event": "omp.unknown"})
        assert valid is False
        assert "不支持" in msg

    def test_normalize_omp_payload_minimal(self):
        """锁定最小 payload 的 NormalizedEvent 输出。"""
        event = normalize_omp_payload(
            {"event": "omp.session_stop", "emittedAt": "2026-07-08T12:00:00.000Z"}
        )
        assert event.provider == "omp"
        assert event.event == "omp.session_stop"
        assert event.title == "会话完成"
        assert event.status == "success"

    def test_normalize_omp_payload_full(self):
        """锁定完整 payload 的所有字段映射。"""
        body = {
            "event": "omp.session_stop",
            "emittedAt": "2026-07-08T12:00:00.000Z",
            "session": {
                "id": "sess_001",
                "name": "Test characterization",
                "file": "/tmp/session.json",
                "cwd": "/tmp",
                "model": "gpt-5.5",
            },
            "round": {
                "turnId": "turn_001",
                "startedAt": "2026-07-08T11:59:00.000Z",
                "endedAt": "2026-07-08T12:00:00.000Z",
                "durationMs": 57700,
                "promptLength": 977,
                "messageCountDelta": 2,
            },
            "metadata": {"version": "0.1.0", "eventName": "session_stop"},
        }
        event = normalize_omp_payload(body)
        assert event.id == "sess_001:turn_001"
        assert event.source["name"] == OMP_SOURCE_NAME
        labels = {f["label"] for f in event.fields}
        assert "会话" in labels
        assert "cwd" in labels
        assert "模型" in labels
