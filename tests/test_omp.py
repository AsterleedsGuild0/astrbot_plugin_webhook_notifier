"""OMP provider tests - no AstrBot dependency."""

from __future__ import annotations

from core.omp import (
    OMP_SOURCE_NAME,
    is_omp_session_stop,
    normalize_omp_payload,
)


class TestIsOmpSessionStop:
    """测试 is_omp_session_stop 事件识别。"""

    def test_header_only(self):
        """仅 Header X-OMP-Event: session_stop 应识别成功。"""
        headers = {"x-omp-event": "session_stop"}
        body = {}
        valid, msg = is_omp_session_stop(headers, body)
        assert valid is True
        assert msg == ""

    def test_header_with_omp_prefix(self):
        """Header X-OMP-Event: omp.session_stop 也应识别。"""
        headers = {"x-omp-event": "omp.session_stop"}
        body = {}
        valid, msg = is_omp_session_stop(headers, body)
        assert valid is True

    def test_body_only(self):
        """仅 Body event: omp.session_stop 应识别成功。"""
        headers = {}
        body = {"event": "omp.session_stop"}
        valid, msg = is_omp_session_stop(headers, body)
        assert valid is True

    def test_body_session_stop_short(self):
        """Body event: session_stop 也应识别。"""
        headers = {}
        body = {"event": "session_stop"}
        valid, msg = is_omp_session_stop(headers, body)
        assert valid is True

    def test_header_and_body_consistent(self):
        """Header 和 Body 同时存在且语义一致应成功。"""
        headers = {"x-omp-event": "session_stop"}
        body = {"event": "omp.session_stop"}
        valid, msg = is_omp_session_stop(headers, body)
        assert valid is True

    def test_header_and_body_inconsistent(self):
        """Header 和 Body 同时存在但不一致应拒绝。"""
        headers = {"x-omp-event": "session_stop"}
        body = {"event": "omp.unknown"}
        valid, msg = is_omp_session_stop(headers, body)
        assert valid is False
        assert "不一致" in msg

    def test_unknown_event(self):
        """未知事件应拒绝。"""
        headers = {}
        body = {"event": "omp.unknown"}
        valid, msg = is_omp_session_stop(headers, body)
        assert valid is False
        assert "不支持" in msg

    def test_empty_body(self):
        """空 body 应无法识别。"""
        headers = {}
        body = {}
        valid, msg = is_omp_session_stop(headers, body)
        assert valid is False

    def test_case_insensitive_header(self):
        """Header key 应大小写不敏感。"""
        headers = {"X-OMP-EVENT": "session_stop"}
        body = {}
        valid, msg = is_omp_session_stop(headers, body)
        assert valid is True


class TestNormalizeOmpPayload:
    """测试 OMP payload 标准化。"""

    def test_minimal_payload(self):
        """最小 payload 也能生成标准化事件。"""
        body = {
            "event": "omp.session_stop",
            "emittedAt": "2026-07-08T12:00:00.000Z",
        }
        event = normalize_omp_payload(body)
        assert event.provider == "omp"
        assert event.event == "omp.session_stop"
        assert event.title != ""
        assert event.status == "success"

    def test_full_payload(self):
        """完整 payload 应正确映射所有字段。"""
        body = {
            "event": "omp.session_stop",
            "version": "1.0",
            "emittedAt": "2026-07-08T12:00:00.000Z",
            "session": {
                "id": "sess_001",
                "name": "Add post-conversation HTTP hook",
                "file": "/home/user/project/session.json",
                "cwd": "/home/user/project",
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
                "stopHookActive": True,
                "lastAssistant": {
                    "provider": "openai",
                    "model": "gpt-5.5",
                    "stopReason": "stop",
                    "timestamp": "2026-07-08T12:00:00.000Z",
                    "durationMs": 57000,
                },
            },
            "metadata": {
                "version": "0.1.0",
                "eventName": "session_stop",
            },
        }
        event = normalize_omp_payload(body)
        assert event.id == "sess_001:turn_001"
        assert event.title == "会话完成"
        assert event.summary == ""
        assert event.source["name"] == OMP_SOURCE_NAME

        # fields 应包含会话、cwd、模型、开始时间、耗时、输入、消息变化、最后状态
        field_labels = {f["label"] for f in event.fields}
        assert "会话" in field_labels
        assert "cwd" in field_labels
        assert "模型" in field_labels
        assert "开始时间" in field_labels
        assert "耗时" in field_labels
        assert "输入" in field_labels
        assert "消息变化" in field_labels
        assert "最后状态" in field_labels
        assert "结束时间" not in field_labels

        # 验证字段值
        for field in event.fields:
            if field["label"] == "会话":
                assert field["value"] == "Add post-conversation HTTP hook"
            elif field["label"] == "cwd":
                assert field["value"] == "/home/user/project"
            elif field["label"] == "模型":
                assert field["value"] == "openai/gpt-5.5"
            elif field["label"] == "开始时间":
                assert field["value"].startswith("2026-07-08 ")
                assert "2026-07-08T" not in field["value"]
                assert (
                    field["value"].endswith(("UTC+00:00", "UTC+08:00"))
                    or "UTC" in field["value"]
                )
            elif field["label"] == "耗时":
                assert "s" in field["value"] or "ms" in field["value"]
            elif field["label"] == "输入":
                assert "977 字" in field["value"]
                assert "1 张图" in field["value"]
            elif field["label"] == "消息变化":
                assert field["value"] == "+2"

        # raw 应包含未映射的字段
        assert event.raw.get("metadata.version") == "0.1.0"
        assert event.raw.get("round.stopHookActive") is True

    def test_summary_does_not_duplicate_session_or_model_fields(self):
        """summary 不应重复展示 fields 中已有的会话和模型。"""
        body = {
            "event": "omp.session_stop",
            "session": {
                "name": "Analyze gpt-5.6 models.yml configuration",
                "model": {"provider": "cpa", "id": "gpt-5.5", "name": "GPT-5.5"},
            },
        }
        event = normalize_omp_payload(body)
        assert event.summary == ""
        assert "Analyze gpt-5.6" not in event.summary
        assert "GPT-5.5" not in event.summary
        assert any(
            f["label"] == "会话"
            and f["value"] == "Analyze gpt-5.6 models.yml configuration"
            for f in event.fields
        )
        assert any(
            f["label"] == "模型" and f["value"] == "cpa/GPT-5.5" for f in event.fields
        )

    def test_session_name_fallback_to_file(self):
        """session.name 缺失时使用 session.file basename。"""
        body = {
            "session": {
                "file": "/home/user/session.json",
            },
        }
        event = normalize_omp_payload(body)
        fields = event.fields
        session_fields = [f for f in fields if f["label"] == "会话"]
        if session_fields:
            assert session_fields[0]["value"] == "session.json"

    def test_session_model_fallback(self):
        """session.model 缺失时使用 round.lastAssistant.model。"""
        body = {
            "session": {},
            "round": {
                "lastAssistant": {
                    "model": "claude-4",
                },
            },
        }
        event = normalize_omp_payload(body)
        model_fields = [f for f in event.fields if f["label"] == "模型"]
        if model_fields:
            assert model_fields[0]["value"] == "claude-4"

    def test_session_model_object_uses_provider_and_name(self):
        """session.model 为对象时优先展示 provider/name。"""
        body = {
            "session": {
                "id": "sess_model_object",
                "model": {
                    "provider": "openai",
                    "id": "gpt-5.5",
                    "name": "GPT-5.5",
                },
            },
        }
        event = normalize_omp_payload(body)
        model_fields = [f for f in event.fields if f["label"] == "模型"]
        assert model_fields[0]["value"] == "openai/GPT-5.5"

    def test_session_model_object_fallback_to_provider_and_id(self):
        """session.model 对象缺少 name 时使用 provider/id。"""
        body = {
            "session": {
                "id": "sess_model_object",
                "model": {
                    "provider": "openai",
                    "id": "gpt-5.5",
                },
            },
        }
        event = normalize_omp_payload(body)
        model_fields = [f for f in event.fields if f["label"] == "模型"]
        assert model_fields[0]["value"] == "openai/gpt-5.5"

    def test_session_model_object_without_provider_uses_name(self):
        """session.model 对象缺少 provider 时仍展示 name。"""
        body = {
            "session": {
                "id": "sess_model_object",
                "model": {
                    "id": "gpt-5.5",
                    "name": "GPT-5.5",
                },
            },
        }
        event = normalize_omp_payload(body)
        model_fields = [f for f in event.fields if f["label"] == "模型"]
        assert model_fields[0]["value"] == "GPT-5.5"

    def test_turn_id_zero_is_preserved(self):
        """round.turnId 为 0 时应保留并参与 event.id。"""
        body = {
            "session": {"id": "sess_zero_turn"},
            "round": {"turnId": 0},
        }
        event = normalize_omp_payload(body)
        assert event.id == "sess_zero_turn:0"

    def test_duration_calculation(self):
        """durationMs 缺失时由 startedAt 和 endedAt 计算。"""
        body = {
            "round": {
                "startedAt": "2026-07-08T11:59:00.000Z",
                "endedAt": "2026-07-08T12:00:00.000Z",
            },
        }
        event = normalize_omp_payload(body)
        duration_fields = [f for f in event.fields if f["label"] == "耗时"]
        if duration_fields:
            assert duration_fields[0]["value"] != ""

    def test_started_at_invalid_format_falls_back_to_raw_value(self):
        """startedAt 无法解析时应回退展示原始值。"""
        body = {
            "round": {
                "startedAt": "not-a-time",
            },
        }
        event = normalize_omp_payload(body)
        started_fields = [f for f in event.fields if f["label"] == "开始时间"]
        assert started_fields[0]["value"] == "not-a-time"

    def test_empty_session(self):
        """session 为空时不崩溃。"""
        body = {
            "session": None,
            "round": None,
        }
        event = normalize_omp_payload(body)
        assert event.title != ""
        assert event.status == "success"

    def test_missing_last_assistant(self):
        """round.lastAssistant 缺失时不崩溃。"""
        body = {
            "session": {"name": "test-session"},
            "round": {},
        }
        event = normalize_omp_payload(body)
        assert event.title != ""
        assert event.status == "success"

    def test_prompt_length_fallback(self):
        """promptLength 缺失但 prompt 存在时使用字符串长度。"""
        body = {
            "session": {"name": "test"},
            "round": {
                "prompt": "hello world " * 100,
            },
        }
        event = normalize_omp_payload(body)
        input_fields = [f for f in event.fields if f["label"] == "输入"]
        if input_fields:
            assert "字" in input_fields[0]["value"]

    def test_emitted_at_fallback(self):
        """emittedAt 缺失时使用请求时间。"""
        body = {}
        request_time = "2026-07-09T10:00:00.000000+00:00"
        event = normalize_omp_payload(body, request_time)
        assert event.emitted_at == request_time

    def test_default_status_success(self):
        """session_stop 默认状态为 success。"""
        body = {"session": {"name": "test"}, "round": {}}
        event = normalize_omp_payload(body)
        assert event.status == "success"

    def test_raw_fields(self):
        """未映射的字段应进入 raw。"""
        body = {
            "session": {"name": "test"},
            "round": {
                "stopHookActive": True,
            },
            "metadata": {
                "version": "1.2.3",
                "eventName": "session_stop",
            },
        }
        event = normalize_omp_payload(body)
        assert event.raw.get("metadata.version") == "1.2.3"
        assert event.raw.get("metadata.eventName") == "session_stop"
        assert event.raw.get("round.stopHookActive") is True
