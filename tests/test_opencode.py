"""OpenCode Provider V1 envelope tests — no AstrBot dependency."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone

import pytest

from core.opencode import (
    OpenCodeProviderAdapter,
    _build_display_name,
    _build_ref12,
    _clean_session_name,
    _check_headers,
    _check_id,
    _check_event,
    _check_version,
    _check_emitted_at,
    _check_session_ref,
    _format_duration_ms,
)
from core.providers import ProviderError
from core.renderer import render_html_data

_VALID_PAYLOAD: dict = {
    "id": "evt_abc123",
    "event": "opencode.session_idle",
    "version": 1,
    "emittedAt": "2026-07-22T12:00:00.000Z",
    "session": {"ref": "sess_secure_abc"},
}

_HEADERS: dict = {"x-opencode-event": "opencode.session_idle"}


def _make_adapter():
    return OpenCodeProviderAdapter()


def _received_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timeline_ref(value: str) -> str:
    return hashlib.sha256(f"opencode:{value}".encode()).hexdigest()[:32]


def _timeline_item(**overrides) -> dict:
    item = {
        "ref": _timeline_ref("child"),
        "parentRef": _timeline_ref("root"),
        "name": "Child",
        "agent": "child-agent",
        "status": "completed",
        "startOffsetMs": 100,
        "endOffsetMs": 400,
        "durationMs": 300,
        "timingQuality": "observed",
        "depth": 1,
        "attempt": 1,
    }
    item.update(overrides)
    return item


def _timeline_payload(
    timeline: dict, *, event: str = "opencode.session_idle", scope="root"
) -> dict:
    return {
        **_VALID_PAYLOAD,
        "event": event,
        "session": {"ref": _timeline_ref("root"), "scope": scope},
        "subagentTimeline": timeline,
    }


def _parse_timeline(payload: dict, headers: dict | None = None):
    return _make_adapter().parse(
        headers=headers or {"x-opencode-event": payload["event"]},
        payload=payload,
        received_at=_received_at(),
    )


def _wait_interval(**overrides) -> dict:
    """合法 complete 用户等待 interval。"""
    interval = {
        "kind": "question",
        "result": "replied",
        "intervalState": "complete",
        "startOffsetMs": 100,
        "endOffsetMs": 500,
        "durationMs": 400,
    }
    interval.update(overrides)
    return interval


def _wait_timeline(**overrides) -> dict:
    """合法 userWaitTimeline envelope（含一个 complete interval）。"""
    timeline = {
        "version": 1,
        "timeBasis": "root_cycle_receipt_monotonic",
        "partial": False,
        "partialReasons": [],
        "observedIntervalCount": 1,
        "displayedIntervalCount": 1,
        "truncated": False,
        "intervals": [_wait_interval()],
    }
    timeline.update(overrides)
    return timeline


def _wait_timeline_payload(
    timeline: dict, *, event: str = "opencode.session_idle", scope: str = "root"
) -> dict:
    return {
        **_VALID_PAYLOAD,
        "event": event,
        "session": {"ref": _timeline_ref("root"), "scope": scope},
        "userWaitTimeline": timeline,
    }


def _wait_right_censored(**overrides) -> dict:
    """合法 right_censored 用户等待 interval（只有 start）。"""
    interval = {
        "kind": "question",
        "intervalState": "right_censored",
        "startOffsetMs": 100,
    }
    interval.update(overrides)
    return interval


def _wait_left_censored(**overrides) -> dict:
    """合法 left_censored 用户等待 interval（result + end，无 start/duration）。"""
    interval = {
        "kind": "permission",
        "result": "replied",
        "intervalState": "left_censored",
        "endOffsetMs": 500,
    }
    interval.update(overrides)
    return interval


# ─── Header/Body 校验 ─────────────────────────────────────


class TestCheckHeaders:
    def test_valid_match(self):
        """Header/Body 一致通过。"""
        h = _check_headers(
            {"x-opencode-event": "opencode.session_idle"},
            {"event": "opencode.session_idle"},
        )
        assert h == "opencode.session_idle"

    def test_header_missing(self):
        """Header 缺失 → ProviderError。"""
        with pytest.raises(ProviderError) as e:
            _check_headers({}, {"event": "opencode.session_idle"})
        assert e.value.code == "invalid_payload"

    def test_body_event_missing(self):
        """Body event 缺失 → ProviderError。"""
        with pytest.raises(ProviderError) as e:
            _check_headers({"x-opencode-event": "opencode.session_idle"}, {})
        assert e.value.code == "invalid_payload"

    def test_mismatch(self):
        """不一致 → event_mismatch。"""
        with pytest.raises(ProviderError) as e:
            _check_headers(
                {"x-opencode-event": "opencode.session_idle"},
                {"event": "opencode.session_error"},
            )
        assert e.value.code == "event_mismatch"

    def test_case_insensitive_header(self):
        """Header 名称大小写不敏感。"""
        h = _check_headers(
            {"X-OpenCode-Event": "opencode.session_idle"},
            {"event": "opencode.session_idle"},
        )
        assert h == "opencode.session_idle"


# ─── 字段校验器 ───────────────────────────────────────────


class TestCheckId:
    def test_valid(self):
        assert _check_id("abc") == "abc"

    def test_empty(self):
        with pytest.raises(ProviderError):
            _check_id("")

    def test_too_long(self):
        with pytest.raises(ProviderError):
            _check_id("a" * 129)

    def test_blank_after_trim(self):
        with pytest.raises(ProviderError):
            _check_id("  ")

    def test_trimmed_returns_trimmed(self):
        assert _check_id("  abc  ") == "abc"


class TestCheckEvent:
    def test_valid(self):
        _check_event("opencode.session_idle")
        _check_event("opencode.session_error")
        _check_event("opencode.permission_asked")
        _check_event("opencode.question_asked")

    def test_unknown(self):
        with pytest.raises(ProviderError) as e:
            _check_event("opencode.unknown")
        assert e.value.code == "unsupported_event"

    def test_empty(self):
        with pytest.raises(ProviderError) as e:
            _check_event("")
        assert e.value.code == "unsupported_event"

    def test_not_string(self):
        with pytest.raises(ProviderError):
            _check_event(123)


class TestCheckVersion:
    def test_valid(self):
        assert _check_version(1) == 1

    def test_bool(self):
        """bool 不是有效 int。"""
        with pytest.raises(ProviderError) as e:
            _check_version(True)
        assert e.value.code == "unsupported_version"

    def test_wrong_int(self):
        with pytest.raises(ProviderError) as e:
            _check_version(2)
        assert e.value.code == "unsupported_version"

    def test_float(self):
        with pytest.raises(ProviderError):
            _check_version(1.0)


class TestCheckEmittedAt:
    def test_valid_z(self):
        r = _check_emitted_at("2026-07-22T12:00:00.000Z")
        assert "T" in r

    def test_valid_offset(self):
        r = _check_emitted_at("2026-07-22T12:00:00+08:00")
        assert "+" in r

    def test_no_tz(self):
        with pytest.raises(ProviderError):
            _check_emitted_at("2026-07-22T12:00:00")

    def test_not_string(self):
        with pytest.raises(ProviderError):
            _check_emitted_at(123)


class TestCheckSessionRef:
    def test_valid(self):
        assert _check_session_ref("ref_abc") == "ref_abc"

    def test_empty(self):
        with pytest.raises(ProviderError):
            _check_session_ref("")

    def test_too_long(self):
        with pytest.raises(ProviderError):
            _check_session_ref("x" * 129)

    def test_blank_after_trim(self):
        with pytest.raises(ProviderError):
            _check_session_ref("  ")

    def test_trimmed_returns_trimmed(self):
        assert _check_session_ref("  ref_abc  ") == "ref_abc"


# ─── Session Name 清洗 ────────────────────────────────────


class TestCleanSessionName:
    def test_normal(self):
        assert _clean_session_name("Hello World") == "Hello World"

    def test_trim(self):
        assert _clean_session_name("  hello  ") == "hello"

    def test_control_chars(self):
        assert _clean_session_name("line1\nline2") == "line1 line2"

    def test_crlf_tab(self):
        assert _clean_session_name("a\r\nb\tc") == "a b c"

    def test_multiple_spaces(self):
        assert _clean_session_name("a    b") == "a b"

    def test_empty_after_clean(self):
        assert _clean_session_name("   ") is None

    def test_none(self):
        assert _clean_session_name(None) is None

    def test_long(self):
        long_str = "x" * 250  # >200
        cleaned = _clean_session_name(long_str)
        assert cleaned is not None
        assert len(cleaned) <= 200


class TestBuildRef12:
    def test_short(self):
        r = _build_ref12("ab")
        assert r == "ab"

    def test_exact_12(self):
        r = _build_ref12("abcdefghijkl")
        assert r == "abcdefghijkl"

    def test_long_replaces(self):
        r = _build_ref12("abcdefghijklmnop")
        assert r == "abcdefghijkl"
        assert len(r) == 12

    def test_special_chars_replaced(self):
        r = _build_ref12("a b_c.d-e!f")
        assert r == "a-b_c.d-e-f"

    def test_unicode(self):
        r = _build_ref12("héllo-wörld")
        assert "-" in r
        # é→-, ö→-, so "h-llo-w-rld" = 11
        assert len(r) == 11

    def test_all_invalid(self):
        r = _build_ref12("!!!@@@###")
        assert r == "---------"
        assert len(r) == 9


class TestBuildDisplayName:
    def test_with_name(self):
        r = _build_display_name("My Session", "ref123")
        assert r == "My Session"

    def test_name_none(self):
        r = _build_display_name(None, "ref_abc_def")
        assert r == "OpenCode Session ref_abc_def"

    def test_name_empty(self):
        r = _build_display_name("", "ref_abc")
        assert r == "OpenCode Session ref_abc"

    def test_ref_short(self):
        r = _build_display_name(None, "ab")
        assert r == "OpenCode Session ab"

    def test_ref_special(self):
        r = _build_display_name(None, "a-b-c.d")
        assert r == "OpenCode Session a-b-c.d"


class TestUnicodeSafety:
    """Unicode Bidi/zero-width/format/separator 不进入 title/source/fields。"""

    def _parse(self, name=None):
        p = dict(_VALID_PAYLOAD)
        if name is not None:
            p["session"] = {"ref": "sess_ref", "name": name}
        return _make_adapter().parse(
            headers=dict(_HEADERS), payload=p, received_at=_received_at()
        )

    def check_cleaned(self, raw, expected_cleaned=None):
        from core.opencode import _clean_session_name

        result = _clean_session_name(raw)
        if expected_cleaned is None:
            assert result is None
        else:
            assert result == expected_cleaned

    def test_rlo_removed(self):
        """U+202E RIGHT-TO-LEFT OVERRIDE 被移除（不替换为空格）。"""
        self.check_cleaned("hello\u202eworld", "helloworld")

    def test_lro_removed(self):
        """U+202D LEFT-TO-RIGHT OVERRIDE 被移除。"""
        self.check_cleaned("abc\u202dxyz", "abcxyz")

    def test_isolate_removed(self):
        """U+2066..U+2069 LRI/RLI/FSI/PDI 被移除。"""
        self.check_cleaned("a\u2066b\u2069c", "abc")

    def test_zwsp_removed(self):
        """U+200B ZERO WIDTH SPACE 被移除。"""
        self.check_cleaned("a\u200bb", "ab")

    def test_zwj_removed(self):
        """U+200D ZERO WIDTH JOINER 被移除。"""
        self.check_cleaned("emoji\u200djoin", "emojijoin")

    def test_bom_removed(self):
        """U+FEFF BOM 被移除。"""
        self.check_cleaned("\ufeffname", "name")

    def test_line_separator_removed(self):
        """U+2028 LINE SEPARATOR 被移除。"""
        self.check_cleaned("line1\u2028line2", "line1line2")

    def test_paragraph_separator_removed(self):
        """U+2029 PARAGRAPH SEPARATOR 被移除。"""
        self.check_cleaned("p1\u2029p2", "p1p2")

    def test_normal_unicode_preserved(self):
        """正常中文/emoji/CJK 保留。"""
        self.check_cleaned("你好世界🚀test", "你好世界🚀test")

    def test_mixed_dangerous_normal(self):
        """混合危险字符时仅移除危险部分。"""
        self.check_cleaned("a\u202eb\u200bc\ufeffd", "abcd")

    def test_title_no_dangerous_chars(self):
        """title 不包含危险 Unicode（集成测试）。"""
        dangerous_name = "test\u202ename\u200bfoo"
        e = self._parse(name=dangerous_name)
        assert "\u202e" not in e.title
        assert "\u200b" not in e.title
        assert "test" in e.title


# ─── OpenCodeProviderAdapter.parse 全量 ───────────────────


class TestOpenCodeProviderAdapterParseSuccess:
    """三事件成功映射。"""

    def _make(self, payload=None, headers=None):
        if payload is None:
            payload = dict(_VALID_PAYLOAD)
        if headers is None:
            headers = dict(_HEADERS)
        return _make_adapter().parse(
            headers=headers, payload=payload, received_at=_received_at()
        )

    def test_session_idle_minimal(self):
        e = self._make()
        assert e.provider == "opencode"
        assert e.event == "opencode.session_idle"
        assert e.id == "evt_abc123"
        assert e.title.startswith("OpenCode Session")
        assert e.source["name"] == "OpenCode"
        assert e.status == "completed"
        assert e.summary == "会话完成"
        assert e.raw == {}
        assert e.version == 1
        assert e.model_variant is None
        fields = {f["label"]: f["value"] for f in e.fields}
        assert fields["sessionName"] == e.title
        assert "modelVariant" not in fields

    def test_session_idle_with_all_optionals(self):
        e = self._make(
            payload={
                "id": "evt_002",
                "event": "opencode.session_idle",
                "version": 1,
                "emittedAt": "2026-07-22T12:00:00.000Z",
                "session": {"ref": "s_ref", "name": "My Task"},
                "agent": "my-agent",
                "model": "gpt-5",
                "modelVariant": "medium",
                "durationMs": 15000,
                "taskStartedAt": "2026-07-22T12:00:00Z",
                "endedAt": "2026-07-22T12:00:15Z",
            }
        )
        assert e.title == "My Task"
        assert e.source["name"] == "OpenCode"
        assert e.status == "completed"
        labels = {f["label"] for f in e.fields}
        assert "agent" in labels
        assert "model" in labels
        assert e.model_variant == "medium"
        assert "modelVariant" in labels
        assert "durationMs" in labels
        assert "sessionRef" in labels

    @pytest.mark.parametrize(
        "variant", ["default", "low", "medium", "high", "max", "experimental"]
    )
    def test_model_variant_is_normalized_and_appended_to_display_model(self, variant):
        event = self._make(
            payload={
                **_VALID_PAYLOAD,
                "model": "cpa/gpt-5.6-sol",
                "modelVariant": variant,
            }
        )
        assert event.model_variant == variant
        raw_fields = {f["label"]: f["value"] for f in event.fields}
        assert raw_fields["modelVariant"] == variant
        display_fields = {
            f["label"]: f["value"] for f in render_html_data(event)["event"]["fields"]
        }
        assert display_fields["模型"] == f"cpa/gpt-5.6-sol({variant})"
        assert "思考深度" not in display_fields

    def test_session_error(self):
        e = self._make(
            payload={
                "id": "evt_err",
                "event": "opencode.session_error",
                "version": 1,
                "emittedAt": "2026-07-22T12:00:00.000Z",
                "session": {"ref": "s_ref"},
                "error": {"category": "timeout", "code": "E001"},
            },
            headers={"x-opencode-event": "opencode.session_error"},
        )
        assert e.event == "opencode.session_error"
        assert e.status == "failed"
        assert e.summary == "会话出错"
        labels = {f["label"] for f in e.fields}
        assert "error.category" in labels
        assert "error.code" in labels
        assert e.fields[labels_index("error.code", e.fields)]["value"] == "E001"

    def test_permission_asked(self):
        e = self._make(
            payload={
                "id": "evt_perm",
                "event": "opencode.permission_asked",
                "version": 1,
                "emittedAt": "2026-07-22T12:00:00.000Z",
                "session": {"ref": "s_ref"},
                "permission": {"category": "file_access"},
            },
            headers={"x-opencode-event": "opencode.permission_asked"},
        )
        assert e.event == "opencode.permission_asked"
        assert e.status == "action_required"
        assert e.summary == "等待权限批准"
        labels = {f["label"] for f in e.fields}
        assert "permissionCount" in labels
        assert "permission[1].category" in labels

    def test_aggregated_permission_contract(self):
        e = self._make(
            payload={
                "id": "evt_perm_aggregate",
                "event": "opencode.permission_asked",
                "version": 1,
                "emittedAt": "2026-07-22T12:00:00.000Z",
                "session": {"ref": "s_ref"},
                "permission": {
                    "count": 2,
                    "items": [
                        {"category": "read"},
                        {"category": "write", "summary": "Write summary"},
                    ],
                },
            },
            headers={"x-opencode-event": "opencode.permission_asked"},
        )
        fields = {f["label"]: f["value"] for f in e.fields}
        assert fields["permissionCount"] == "2"
        assert fields["permission[1].category"] == "read"
        assert fields["permission[2].summary"] == "Write summary"

    def test_question_asked(self):
        e = self._make(
            payload={
                "id": "evt_question",
                "event": "opencode.question_asked",
                "version": 1,
                "emittedAt": "2026-07-22T12:00:00.000Z",
                "session": {"ref": "s_ref"},
            },
            headers={"x-opencode-event": "opencode.question_asked"},
        )
        assert e.event == "opencode.question_asked"
        assert e.status == "action_required"
        assert e.summary == "等待问题回答"
        assert e.raw == {}
        labels = {f["label"] for f in e.fields}
        assert "sessionRef" in labels
        assert "permission.category" not in labels
        assert "error.category" not in labels

    def test_rich_fields_reach_final_event_fields(self):
        e = self._make(
            payload={
                "id": "evt_rich",
                "event": "opencode.question_asked",
                "version": 1,
                "emittedAt": "2026-07-22T12:00:00.000Z",
                "session": {"ref": "s_ref", "name": "Session One"},
                "instanceDisplayName": "Demo Instance",
                "projectName": "demo-project",
                "agent": "build-agent",
                "model": "openai/gpt-5",
                "durationMs": 65000,
                "startedAt": "2026-07-22T12:00:00Z",
                "taskStartedAt": "2026-07-22T12:00:00Z",
                "endedAt": "2026-07-22T12:01:05Z",
                "counts": {"messages": 3, "tools": 2, "changes": 1},
                "question": {
                    "count": 1,
                    "optionCount": 2,
                    "summary": "Choose an environment",
                    "items": [
                        {
                            "text": "Choose an environment",
                            "header": "Environment",
                            "recommended": "staging",
                            "options": [
                                {
                                    "label": "Production",
                                    "description": "Deploy to production",
                                    "recommended": False,
                                },
                                {
                                    "label": "Staging",
                                    "description": "Deploy to staging",
                                    "recommended": True,
                                },
                            ],
                        }
                    ],
                },
            },
            headers={"x-opencode-event": "opencode.question_asked"},
        )
        fields = {f["label"]: f["value"] for f in e.fields}
        assert e.source["name"] == "Demo Instance"
        assert fields["projectName"] == "demo-project"
        assert fields["sessionName"] == "Session One"
        assert fields["model"] == "openai/gpt-5"
        assert fields["duration"] == "1m 5s"
        assert fields["startedAt"] == "2026-07-22T12:00:00+00:00"
        assert fields["taskStartedAt"] == "2026-07-22T12:00:00+00:00"
        assert fields["endedAt"] == "2026-07-22T12:01:05+00:00"
        assert fields["messageCount"] == "3"
        assert fields["toolCount"] == "2"
        assert fields["changeCount"] == "1"
        assert fields["questionCount"] == "1"
        assert fields["optionCount"] == "2"
        assert "Deploy to production" in fields["question[1].options"]

    def test_instance_source_and_project_detail_are_separate(self):
        event = self._make(
            payload={
                **_VALID_PAYLOAD,
                "session": {"ref": "s_ref", "name": "Session One"},
                "instanceDisplayName": "OpenCode Desktop",
                "projectName": "actual-project",
            }
        )
        fields = {f["label"]: f["value"] for f in event.fields}
        assert event.source["name"] == "OpenCode Desktop"
        assert fields["projectName"] == "actual-project"
        assert fields["sessionName"] == "Session One"

    def test_unknown_legacy_instance_field_is_rejected(self):
        legacy_field = "project" + "DisplayName"
        with pytest.raises(ProviderError) as error:
            self._make(payload={**_VALID_PAYLOAD, legacy_field: "Legacy Instance"})
        assert error.value.code == "invalid_payload"
        assert str(error.value) == "不允许的字段"

    def test_rich_permission_fields_are_allowlisted(self):
        e = self._make(
            payload={
                "id": "evt_perm_rich",
                "event": "opencode.permission_asked",
                "version": 1,
                "emittedAt": "2026-07-22T12:00:00.000Z",
                "session": {"ref": "s_ref"},
                "permission": {
                    "category": "file_access",
                    "title": "Read file",
                    "summary": "Read requested file",
                    "description": "Allow the requested file read",
                    "action": "read",
                    "target": "/private/project/file.txt",
                    "patterns": ["/private/project/**"],
                },
            },
            headers={"x-opencode-event": "opencode.permission_asked"},
        )
        fields = {f["label"]: f["value"] for f in e.fields}
        assert fields["permission[1].category"] == "file_access"
        assert fields["permission[1].title"] == "Read file"
        assert fields["permission[1].description"] == "Allow the requested file read"
        assert fields["permission[1].action"] == "read"
        assert fields["permission[1].target"] == "/private/project/file.txt"
        assert fields["permission[1].patterns"] == "/private/project/**"
        assert e.raw == {}

    def test_card_display_localizes_fields_without_changing_envelope_keys(self):
        e = self._make(
            payload={
                "id": "evt_display",
                "event": "opencode.question_asked",
                "version": 1,
                "emittedAt": "2026-07-23T17:45:35+08:00",
                "session": {"ref": "s_ref", "name": "Session One"},
                "durationMs": 211_020_000,
                "startedAt": "2026-07-23T17:44:35+08:00",
                "taskStartedAt": "2026-07-23T17:44:35+08:00",
                "endedAt": "2026-07-26T04:21:35+08:00",
                "question": {
                    "count": 1,
                    "items": [
                        {
                            "header": "确认",
                            "text": "继续吗？",
                            "options": [
                                {"label": "继续", "recommended": True},
                                {"label": "取消", "recommended": False},
                            ],
                        }
                    ],
                },
            },
            headers={"x-opencode-event": "opencode.question_asked"},
        )
        raw_fields = {field["label"]: field["value"] for field in e.fields}
        assert raw_fields["durationMs"] == "211020000"
        assert raw_fields["startedAt"] == "2026-07-23T17:44:35+08:00"

        display = render_html_data(e)["event"]
        display_fields = {field["label"]: field["value"] for field in display["fields"]}
        assert display["status_display"] == "待处理"
        assert display_fields["当前任务耗时"] == "2 天 10 小时 37 分钟"
        assert display_fields["会话开始时间"] == "2026-07-23 17:44:35 CST (UTC+08:00)"
        assert all(field["label"] != "durationMs" for field in display["fields"])
        assert display_fields["问题 1 选项"] == "1. 继续（推荐）\n2. 取消"

    @pytest.mark.parametrize(
        "session_name, instance_name, expected_title, expected_source",
        [
            ("Task", "OpenCode Desktop", "Task", "OpenCode Desktop"),
            (None, "OpenCode Desktop", "OpenCode Session s_ref", "OpenCode Desktop"),
            ("Task", None, "Task", "OpenCode"),
        ],
    )
    def test_title_source_and_session_name_contract(
        self, session_name, instance_name, expected_title, expected_source
    ):
        payload = {
            **_VALID_PAYLOAD,
            "session": {"ref": "s_ref"},
        }
        if session_name is not None:
            payload["session"]["name"] = session_name
        if instance_name is not None:
            payload["instanceDisplayName"] = instance_name

        event = self._make(payload=payload)
        fields = {f["label"]: f["value"] for f in event.fields}
        assert event.title == expected_title
        assert event.source["name"] == expected_source
        assert fields["sessionName"] == expected_title


class TestOpenCodeFormatting:
    @pytest.mark.parametrize(
        "duration_ms, expected",
        [(0, "0ms"), (999, "999ms"), (1000, "1s"), (65000, "1m 5s"), (3600000, "1h")],
    )
    def test_duration_is_readable(self, duration_ms, expected):
        assert _format_duration_ms(duration_ms) == expected


class TestSubagentTimelineContract:
    def test_root_cycle_duration_and_timeline_round_trip_together(self):
        timeline = {
            "version": 1,
            "partial": False,
            "partialReasons": [],
            "timeBasis": "root_cycle",
            "observedItemCount": 1,
            "displayedItemCount": 1,
            "truncated": False,
            "items": [_timeline_item()],
        }
        payload = _timeline_payload(timeline)
        payload["durationMs"] = 15000

        event = _parse_timeline(payload)

        assert event.task_duration_ms == 15000
        assert event.subagent_timeline == timeline

    def test_direct_nested_partial_and_truncated_round_trip(self):
        timelines = [
            {
                "version": 1,
                "partial": False,
                "partialReasons": [],
                "timeBasis": "root_cycle",
                "observedItemCount": 1,
                "displayedItemCount": 1,
                "truncated": False,
                "items": [_timeline_item()],
            },
            {
                "version": 1,
                "partial": False,
                "partialReasons": [],
                "timeBasis": "root_cycle",
                "observedItemCount": 2,
                "displayedItemCount": 2,
                "truncated": False,
                "items": [
                    _timeline_item(
                        ref=_timeline_ref("grandchild"),
                        parentRef=_timeline_ref("child"),
                        depth=2,
                    ),
                    _timeline_item(),
                ],
            },
            {
                "version": 1,
                "partial": True,
                "partialReasons": ["missing_start", "missing_end"],
                "timeBasis": "root_cycle",
                "observedItemCount": 1,
                "displayedItemCount": 1,
                "truncated": False,
                "items": [],
            },
            {
                "version": 1,
                "partial": True,
                "partialReasons": ["truncated"],
                "timeBasis": "root_cycle",
                "observedItemCount": 2,
                "displayedItemCount": 1,
                "truncated": True,
                "items": [_timeline_item()],
            },
        ]

        partial_item = _timeline_item(timingQuality="unknown")
        partial_item.pop("startOffsetMs")
        partial_item.pop("endOffsetMs")
        partial_item.pop("durationMs")
        timelines[2]["items"] = [partial_item]

        for timeline in timelines:
            event = _parse_timeline(_timeline_payload(timeline))
            assert event.subagent_timeline == timeline
            assert event.to_dict()["subagent_timeline"] == timeline

    def test_optional_model_metadata_round_trip(self):
        timeline = {
            "version": 1,
            "partial": False,
            "partialReasons": [],
            "timeBasis": "root_cycle",
            "observedItemCount": 1,
            "displayedItemCount": 1,
            "truncated": False,
            "items": [_timeline_item(model="provider/model", modelVariant="xhigh")],
        }
        event = _parse_timeline(_timeline_payload(timeline))
        assert event.subagent_timeline == timeline

    def test_missing_optional_timeline_is_compatible(self):
        event = _make_adapter().parse(
            headers=dict(_HEADERS),
            payload=dict(_VALID_PAYLOAD),
            received_at=_received_at(),
        )
        assert event.subagent_timeline is None
        assert "subagent_timeline" not in event.to_dict()

    @pytest.mark.parametrize(
        ("event", "scope"),
        [
            ("opencode.question_asked", "root"),
            ("opencode.session_idle", "subagent"),
            ("opencode.session_idle", "auxiliary"),
            ("opencode.session_idle", "unknown"),
        ],
    )
    def test_timeline_is_root_idle_only(self, event, scope):
        payload = _timeline_payload(
            {
                "version": 1,
                "partial": False,
                "partialReasons": [],
                "timeBasis": "root_cycle",
                "observedItemCount": 0,
                "displayedItemCount": 0,
                "truncated": False,
                "items": [],
            },
            event=event,
            scope=scope,
        )
        with pytest.raises(ProviderError):
            _parse_timeline(payload)

    @pytest.mark.parametrize(
        "mutation",
        [
            lambda t: t.update(extra=True),
            lambda t: t.update(version=2),
            lambda t: t.update(partial=False, partialReasons=["missing_end"]),
            lambda t: t.update(partialReasons=["missing_end", "missing_end"]),
            lambda t: t.update(partialReasons=["not-a-reason"], partial=True),
            lambda t: t.update(timeBasis="wall_clock"),
            lambda t: t.update(observedItemCount=-1),
            lambda t: t.update(displayedItemCount=2),
            lambda t: t.update(items=[]),
            lambda t: t.update(truncated=True, partial=False, partialReasons=[]),
        ],
    )
    def test_invalid_timeline_envelope_is_rejected(self, mutation):
        timeline = {
            "version": 1,
            "partial": False,
            "partialReasons": [],
            "timeBasis": "root_cycle",
            "observedItemCount": 1,
            "displayedItemCount": 1,
            "truncated": False,
            "items": [_timeline_item()],
        }
        mutation(timeline)
        with pytest.raises(ProviderError):
            _parse_timeline(_timeline_payload(timeline))

    @pytest.mark.parametrize(
        "mutation",
        [
            lambda item: item.update(extra=True),
            lambda item: item.update(ref="raw-session-id"),
            lambda item: item.update(parentRef="/private/project"),
            lambda item: item.update(status="done"),
            lambda item: item.update(timingQuality="complete"),
            lambda item: item.update(name="x" * 201),
            lambda item: item.update(agent="x" * 129),
            lambda item: item.update(model=123),
            lambda item: item.update(model=""),
            lambda item: item.update(model="x" * 129),
            lambda item: item.update(modelVariant=123),
            lambda item: item.update(modelVariant=" " * 2),
            lambda item: item.update(modelVariant="x" * 129),
            lambda item: item.update(startOffsetMs=-1),
            lambda item: item.update(endOffsetMs=99, durationMs=1),
            lambda item: item.update(depth=0),
            lambda item: item.update(depth=9),
            lambda item: item.update(attempt=0),
            lambda item: item.update(attempt=1_000_001),
            lambda item: item.update(startOffsetMs=math.inf),
            lambda item: item.update(startOffsetMs=200, endOffsetMs=100),
        ],
    )
    def test_invalid_timeline_item_is_rejected(self, mutation):
        timeline = {
            "version": 1,
            "partial": False,
            "partialReasons": [],
            "timeBasis": "root_cycle",
            "observedItemCount": 1,
            "displayedItemCount": 1,
            "truncated": False,
            "items": [_timeline_item()],
        }
        mutation(timeline["items"][0])
        with pytest.raises(ProviderError):
            _parse_timeline(_timeline_payload(timeline))

    def test_partial_timing_rejects_exact_duration(self):
        timeline = {
            "version": 1,
            "partial": True,
            "partialReasons": ["clamped"],
            "timeBasis": "root_cycle",
            "observedItemCount": 1,
            "displayedItemCount": 1,
            "truncated": False,
            "items": [
                _timeline_item(
                    timingQuality="partial",
                )
            ],
        }
        with pytest.raises(ProviderError):
            _parse_timeline(_timeline_payload(timeline))

    def test_accepts_sixty_four_items_and_rejects_more(self):
        items = [
            _timeline_item(
                ref=_timeline_ref(f"child-{index}"),
                name=f"Child {index}",
                startOffsetMs=index,
                endOffsetMs=index + 1,
                durationMs=1,
            )
            for index in range(64)
        ]
        timeline = {
            "version": 1,
            "partial": False,
            "partialReasons": [],
            "timeBasis": "root_cycle",
            "observedItemCount": 64,
            "displayedItemCount": 64,
            "truncated": False,
            "items": items,
        }
        assert (
            _parse_timeline(_timeline_payload(timeline)).subagent_timeline == timeline
        )

        too_many = dict(timeline)
        too_many["observedItemCount"] = 65
        too_many["displayedItemCount"] = 65
        too_many["items"] = items + [_timeline_item(ref=_timeline_ref("child-64"))]
        with pytest.raises(ProviderError):
            _parse_timeline(_timeline_payload(too_many))

    def test_body_size_limit_still_applies_with_timeline(self):
        timeline = {
            "version": 1,
            "partial": False,
            "partialReasons": [],
            "timeBasis": "root_cycle",
            "observedItemCount": 0,
            "displayedItemCount": 0,
            "truncated": False,
            "items": [],
        }
        with pytest.raises(ProviderError):
            _parse_timeline(
                {
                    **_timeline_payload(timeline),
                    "projectName": "x" * (65 * 1024),
                }
            )

    def test_timeline_size_limit_counts_model_metadata(self):
        items = [
            _timeline_item(
                ref=_timeline_ref(f"model-size-{index}"),
                name=f"Child {index}",
                model="m" * 128,
                modelVariant="v" * 128,
                startOffsetMs=index,
                endOffsetMs=index + 1,
                durationMs=1,
            )
            for index in range(64)
        ]
        timeline = {
            "version": 1,
            "partial": False,
            "partialReasons": [],
            "timeBasis": "root_cycle",
            "observedItemCount": 64,
            "displayedItemCount": 64,
            "truncated": False,
            "items": items,
        }
        with pytest.raises(ProviderError):
            _parse_timeline(_timeline_payload(timeline))


# ─── userWaitTimeline 契约 ─────────────────────────────────


class TestUserWaitTimelineContract:
    """userWaitTimeline 服务端兼容层（Issue #26，Phase 1 server lane）。

    wire contract：顶层 camelCase ``userWaitTimeline`` → Python normalized 字段
    ``user_wait_timeline``。旧 envelope 完全不携带该字段时兼容，不报错。
    """

    # ── 合法 payload ──────────────────────────────────────

    def test_empty_timeline_is_reliable_zero(self):
        """空 intervals + partial=false = 可靠测得 0，与字段缺失可区分。"""
        timeline = _wait_timeline(
            partial=False,
            partialReasons=[],
            observedIntervalCount=0,
            displayedIntervalCount=0,
            truncated=False,
            intervals=[],
        )
        event = _parse_timeline(_wait_timeline_payload(timeline))
        assert event.user_wait_timeline == timeline
        assert event.to_dict()["user_wait_timeline"] == timeline

    def test_complete_interval_round_trip(self):
        timeline = _wait_timeline()
        event = _parse_timeline(_wait_timeline_payload(timeline))
        assert event.user_wait_timeline == timeline
        assert event.to_dict()["user_wait_timeline"] == timeline

    def test_permission_kind_and_rejected_result_round_trip(self):
        timeline = _wait_timeline(
            intervals=[
                _wait_interval(kind="permission", result="rejected"),
            ]
        )
        event = _parse_timeline(_wait_timeline_payload(timeline))
        assert event.user_wait_timeline == timeline

    def test_mixed_complete_and_censored_intervals_round_trip(self):
        """complete + right_censored + left_censored 同时出现，censored 需要 partial。"""
        timeline = _wait_timeline(
            partial=True,
            partialReasons=["open_at_cycle_end", "orphan_resolution"],
            observedIntervalCount=3,
            displayedIntervalCount=3,
            truncated=False,
            intervals=[
                _wait_interval(),
                _wait_right_censored(),
                _wait_left_censored(),
            ],
        )
        event = _parse_timeline(_wait_timeline_payload(timeline))
        assert event.user_wait_timeline == timeline

    def test_observed_greater_than_displayed_with_evicted_reason(self):
        """observed>displayed 可由 evicted reason 解释，不需要 truncated。"""
        timeline = _wait_timeline(
            partial=True,
            partialReasons=["evicted"],
            observedIntervalCount=2,
            displayedIntervalCount=1,
            truncated=False,
            intervals=[_wait_interval()],
        )
        event = _parse_timeline(_wait_timeline_payload(timeline))
        normalized = event.user_wait_timeline
        assert normalized is not None
        assert normalized["observedIntervalCount"] == 2
        assert normalized["displayedIntervalCount"] == 1

    def test_missing_timeline_is_compatible(self):
        """旧 envelope：完全不带 userWaitTimeline 不报错，字段为 None 且不进 to_dict。"""
        event = _make_adapter().parse(
            headers=dict(_HEADERS),
            payload=dict(_VALID_PAYLOAD),
            received_at=_received_at(),
        )
        assert event.user_wait_timeline is None
        assert "user_wait_timeline" not in event.to_dict()

    def test_both_timelines_can_coexist_independently(self):
        """subagentTimeline 与 userWaitTimeline 同 payload 共存，互不污染。"""
        subagent = {
            "version": 1,
            "partial": False,
            "partialReasons": [],
            "timeBasis": "root_cycle",
            "observedItemCount": 1,
            "displayedItemCount": 1,
            "truncated": False,
            "items": [_timeline_item()],
        }
        user_wait = _wait_timeline()
        payload = _wait_timeline_payload(user_wait)
        payload["subagentTimeline"] = subagent
        event = _parse_timeline(payload)
        assert event.subagent_timeline == subagent
        assert event.user_wait_timeline == user_wait

    def test_offsets_beyond_root_duration_accepted(self):
        """遵循既有 subagentTimeline 兼容模式：adapter 接受越界 offset，不校验
        offset <= top-level durationMs；越界由 renderer 防御性裁剪到 [0, duration]。
        （与 ``_validate_subagent_timeline`` 行为一致，避免同轴双 timeline 契约分叉。）"""
        timeline = _wait_timeline(
            intervals=[
                _wait_interval(startOffsetMs=15000, endOffsetMs=16000, durationMs=1000),
            ]
        )
        payload = _wait_timeline_payload(timeline)
        payload["durationMs"] = 10000
        event = _parse_timeline(payload)
        assert event.task_duration_ms == 10000
        normalized = event.user_wait_timeline
        assert normalized is not None
        assert normalized["intervals"][0]["endOffsetMs"] == 16000

    def test_zero_duration_and_offset_boundaries(self):
        """零值 offset、duration 合法：start=end=duration=0。"""
        timeline = _wait_timeline(
            intervals=[
                _wait_interval(startOffsetMs=0, endOffsetMs=0, durationMs=0),
            ]
        )
        event = _parse_timeline(_wait_timeline_payload(timeline))
        normalized = event.user_wait_timeline
        assert normalized is not None
        assert normalized["intervals"][0]["durationMs"] == 0

    # ── event / scope 限制 ────────────────────────────────

    @pytest.mark.parametrize(
        ("event", "scope"),
        [
            ("opencode.question_asked", "root"),
            ("opencode.permission_asked", "root"),
            ("opencode.session_error", "root"),
            ("opencode.session_idle", "subagent"),
            ("opencode.session_idle", "auxiliary"),
            ("opencode.session_idle", "unknown"),
        ],
    )
    def test_user_wait_timeline_is_root_idle_only(self, event, scope):
        payload = _wait_timeline_payload(_wait_timeline(), event=event, scope=scope)
        with pytest.raises(ProviderError):
            _parse_timeline(payload)

    # ── envelope 非法组合 ─────────────────────────────────

    @pytest.mark.parametrize(
        "mutation",
        [
            lambda t: t.update(extra=True),
            lambda t: t.update(version=2),
            lambda t: t.update(version=True),
            lambda t: t.update(timeBasis="wall_clock"),
            lambda t: t.update(partial=True, partialReasons=[]),
            lambda t: t.update(partial=False, partialReasons=["clock_invalid"]),
            lambda t: t.update(partialReasons=["clock_invalid", "clock_invalid"]),
            lambda t: t.update(partialReasons=["not-a-reason"], partial=True),
            lambda t: t.update(
                partialReasons=["clock_invalid", "evicted"], partial=True
            ),
            lambda t: t.update(observedIntervalCount=-1),
            lambda t: t.update(observedIntervalCount=1.5),
            lambda t: t.update(observedIntervalCount=True),
            lambda t: t.update(displayedIntervalCount=2),
            lambda t: t.update(intervals=[]),
            lambda t: t.update(intervals="not-a-list"),
            lambda t: t.update(truncated=True, partial=False, partialReasons=[]),
            lambda t: t.update(
                truncated=True, partial=True, partialReasons=["truncated"]
            ),
            lambda t: t.update(
                truncated=True,
                partial=True,
                partialReasons=["truncated"],
                observedIntervalCount=1,
                displayedIntervalCount=1,
            ),
            lambda t: t.update(
                truncated=False, partial=True, partialReasons=["truncated"]
            ),
        ],
    )
    def test_invalid_user_wait_envelope_rejected(self, mutation):
        timeline = _wait_timeline()
        mutation(timeline)
        with pytest.raises(ProviderError):
            _parse_timeline(_wait_timeline_payload(timeline))

    # ── interval 非法组合 ─────────────────────────────────

    @pytest.mark.parametrize(
        "mutation",
        [
            # unknown / sensitive 字段
            lambda i: i.update(extra=True),
            lambda i: i.update(raw="leak"),
            lambda i: i.update(sessionId="leak"),
            lambda i: i.update(requestId="leak"),
            lambda i: i.update(text="leak"),
            # kind / state / result 枚举
            lambda i: i.update(kind="notice"),
            lambda i: i.update(kind=123),
            lambda i: i.update(intervalState="partial"),
            lambda i: i.update(intervalState="censored"),
            lambda i: i.update(result="answered"),
            lambda i: i.update(result=123),
            lambda i: i.update(result=None),
            # offset 类型
            lambda i: i.update(startOffsetMs=-1),
            lambda i: i.update(startOffsetMs=True),
            lambda i: i.update(startOffsetMs=1.5),
            lambda i: i.update(startOffsetMs=math.nan),
            lambda i: i.update(startOffsetMs=math.inf),
            lambda i: i.update(endOffsetMs=-1),
            lambda i: i.update(durationMs=-1),
            lambda i: i.update(durationMs=True),
            # 组合错误：complete
            lambda i: i.update(startOffsetMs=200, endOffsetMs=100),
            lambda i: i.update(durationMs=500),
            lambda i: i.update(durationMs=999),
            lambda i: i.update(endOffsetMs=999, durationMs=1),
        ],
    )
    def test_invalid_complete_interval_rejected(self, mutation):
        timeline = _wait_timeline()
        mutation(timeline["intervals"][0])
        with pytest.raises(ProviderError):
            _parse_timeline(_wait_timeline_payload(timeline))

    def test_complete_interval_requires_exact_key_set(self):
        """complete 缺少任何 required 字段（含 result）都拒绝。"""
        cases = [
            _wait_interval(),
        ]
        for key in (
            "kind",
            "result",
            "intervalState",
            "startOffsetMs",
            "endOffsetMs",
            "durationMs",
        ):
            item = _wait_interval()
            del item[key]
            cases.append(item)
        for item in cases[1:]:
            timeline = _wait_timeline(intervals=[item])
            with pytest.raises(ProviderError):
                _parse_timeline(_wait_timeline_payload(timeline))

    def test_right_censored_requires_only_start(self):
        """right_censored：只允许 kind/intervalState/start，多余字段拒绝。"""
        extra_cases = [
            _wait_right_censored(result="replied"),
            _wait_right_censored(endOffsetMs=500),
            _wait_right_censored(durationMs=400),
        ]
        for item in extra_cases:
            timeline = _wait_timeline(
                partial=True,
                partialReasons=["open_at_cycle_end"],
                intervals=[item],
            )
            with pytest.raises(ProviderError):
                _parse_timeline(_wait_timeline_payload(timeline))

    def test_left_censored_requires_result_and_end(self):
        """left_censored：必须有 result+end，多余/缺失字段拒绝。"""
        missing_result = _wait_left_censored()
        del missing_result["result"]
        missing_end = _wait_left_censored()
        del missing_end["endOffsetMs"]
        invalid_cases = [
            missing_result,
            missing_end,
            _wait_left_censored(startOffsetMs=0),
            _wait_left_censored(durationMs=500),
            _wait_left_censored(result=None),
            _wait_left_censored(result="answered"),
        ]
        for item in invalid_cases:
            timeline = _wait_timeline(
                partial=True,
                partialReasons=["orphan_resolution"],
                intervals=[item],
            )
            with pytest.raises(ProviderError):
                _parse_timeline(_wait_timeline_payload(timeline))

    def test_censored_interval_requires_partial(self):
        """censored interval 存在但 partial=false → 拒绝。"""
        for item, reason in (
            (_wait_right_censored(), "open_at_cycle_end"),
            (_wait_left_censored(), "orphan_resolution"),
        ):
            timeline = _wait_timeline(
                partial=False,
                partialReasons=[],
                observedIntervalCount=1,
                displayedIntervalCount=1,
                truncated=False,
                intervals=[item],
            )
            with pytest.raises(ProviderError):
                _parse_timeline(_wait_timeline_payload(timeline))

    def test_reason_binding_requires_matching_censored_state(self):
        """open_at_cycle_end → 至少一个 right_censored；orphan_resolution → 至少一个
        left_censored；只有 complete interval 时携带这些 reason 拒绝。"""
        # open_at_cycle_end 但只有 complete
        timeline = _wait_timeline(
            partial=True,
            partialReasons=["open_at_cycle_end"],
            intervals=[_wait_interval()],
        )
        with pytest.raises(ProviderError):
            _parse_timeline(_wait_timeline_payload(timeline))
        # orphan_resolution 但只有 complete
        timeline = _wait_timeline(
            partial=True,
            partialReasons=["orphan_resolution"],
            intervals=[_wait_interval()],
        )
        with pytest.raises(ProviderError):
            _parse_timeline(_wait_timeline_payload(timeline))
        # open_at_cycle_end 配 right_censored → 合法
        timeline = _wait_timeline(
            partial=True,
            partialReasons=["open_at_cycle_end"],
            intervals=[_wait_right_censored()],
        )
        event = _parse_timeline(_wait_timeline_payload(timeline))
        normalized = event.user_wait_timeline
        assert normalized is not None
        assert normalized["intervals"][0]["intervalState"] == "right_censored"

    def test_truncated_allows_open_reason_without_displayed_right_censored(self):
        """Oracle Gate1 P1：truncated=true 时 open_at_cycle_end 对应 right_censored
        可能已被截断出 displayed 列表（observed=2/displayed=1），允许 reason 存在。"""
        timeline = _wait_timeline(
            partial=True,
            partialReasons=["open_at_cycle_end", "truncated"],
            observedIntervalCount=2,
            displayedIntervalCount=1,
            truncated=True,
            intervals=[_wait_interval()],
        )
        event = _parse_timeline(_wait_timeline_payload(timeline))
        assert event.user_wait_timeline is not None

    def test_truncated_allows_orphan_reason_without_displayed_left_censored(self):
        """Oracle Gate1 P1：truncated=true 时 orphan_resolution 对应 left_censored
        可能已被截断出 displayed 列表，允许 reason 存在。"""
        timeline = _wait_timeline(
            partial=True,
            partialReasons=["orphan_resolution", "truncated"],
            observedIntervalCount=2,
            displayedIntervalCount=1,
            truncated=True,
            intervals=[_wait_interval()],
        )
        event = _parse_timeline(_wait_timeline_payload(timeline))
        assert event.user_wait_timeline is not None

    def test_truncated_relaxation_not_applied_when_not_truncated(self):
        """truncated=false 时保留严格绑定：无对应 censored 携带 open/orphan reason 拒绝
        （同 payload 去掉 truncated reason 即回归严格）。"""
        for reason in ("open_at_cycle_end", "orphan_resolution"):
            timeline = _wait_timeline(
                partial=True,
                partialReasons=[reason],
                observedIntervalCount=1,
                displayedIntervalCount=1,
                truncated=False,
                intervals=[_wait_interval()],
            )
            with pytest.raises(ProviderError):
                _parse_timeline(_wait_timeline_payload(timeline))

    def test_truncated_with_displayed_censored_still_accepted(self):
        """truncated=true 且 displayed 仍包含对应 censored interval → 依然接受。"""
        timeline = _wait_timeline(
            partial=True,
            partialReasons=["open_at_cycle_end", "truncated"],
            observedIntervalCount=3,
            displayedIntervalCount=2,
            truncated=True,
            intervals=[_wait_right_censored(), _wait_interval()],
        )
        event = _parse_timeline(_wait_timeline_payload(timeline))
        normalized = event.user_wait_timeline
        assert normalized is not None
        assert normalized["intervals"][0]["intervalState"] == "right_censored"

    def test_truncated_does_not_weaken_censored_requires_partial(self):
        """截断放宽只作用于「reason ↔ displayed 缺失」；present censored interval
        仍需 partial=true（无 partial reason 的 censored 依旧拒绝）。"""
        timeline = _wait_timeline(
            partial=False,
            partialReasons=[],
            observedIntervalCount=1,
            displayedIntervalCount=1,
            truncated=False,
            intervals=[_wait_right_censored()],
        )
        with pytest.raises(ProviderError):
            _parse_timeline(_wait_timeline_payload(timeline))
        # truncated=true 也要求 partial=true（truncated 本身强制 partial + reason）
        timeline = _wait_timeline(
            partial=True,
            partialReasons=["truncated"],
            observedIntervalCount=2,
            displayedIntervalCount=1,
            truncated=True,
            intervals=[_wait_right_censored()],
        )
        event = _parse_timeline(_wait_timeline_payload(timeline))
        assert event.user_wait_timeline is not None

    def test_reason_canonical_order_required(self):
        """partialReasons 必须按 canonical 顺序；乱序拒绝（与 subagentTimeline 一致）。"""
        timeline = _wait_timeline(
            partial=True,
            partialReasons=["clock_invalid", "evicted"],
            observedIntervalCount=2,
            displayedIntervalCount=1,
            intervals=[_wait_interval()],
        )
        with pytest.raises(ProviderError):
            _parse_timeline(_wait_timeline_payload(timeline))

    # ── count / 数量 / 大小限制 ───────────────────────────

    def test_accepts_sixty_four_intervals_and_rejects_more(self):
        intervals = [
            _wait_interval(
                startOffsetMs=index,
                endOffsetMs=index + 1,
                durationMs=1,
            )
            for index in range(64)
        ]
        timeline = _wait_timeline(
            observedIntervalCount=64,
            displayedIntervalCount=64,
            intervals=intervals,
        )
        assert (
            _parse_timeline(_wait_timeline_payload(timeline)).user_wait_timeline
            == timeline
        )

        too_many = dict(timeline)
        too_many["observedIntervalCount"] = 65
        too_many["displayedIntervalCount"] = 65
        too_many["intervals"] = intervals + [_wait_interval()]
        with pytest.raises(ProviderError):
            _parse_timeline(_wait_timeline_payload(too_many))

    def test_twelve_kib_timeline_size_limit_is_defensive(self, monkeypatch):
        """独立 canonical JSON ≤12KiB 是防御性上限：当前严格 schema（枚举字符串 +
        有界整数 offset）下 64 个合法 interval 的最大体积约 9.7KiB，永远无法触达
        12KiB。因此验证方式是把上限临时收紧，确认检查机制确实生效。"""
        import core.opencode as opencode_module

        # 64 个最大体积合法 interval（16 位 offset）应被接受
        max_int = 2**53 - 1
        intervals = [
            _wait_interval(
                startOffsetMs=max_int - index,
                endOffsetMs=max_int,
                durationMs=index,
            )
            for index in range(64)
        ]
        timeline = _wait_timeline(
            observedIntervalCount=64,
            displayedIntervalCount=64,
            intervals=intervals,
        )
        assert (
            _parse_timeline(_wait_timeline_payload(timeline)).user_wait_timeline
            is not None
        )

        # 收紧上限后，正常大小的 timeline 也应被拒绝 → 证明字节检查生效
        monkeypatch.setattr(opencode_module, "_MAX_USER_WAIT_TIMELINE_BYTES", 100)
        with pytest.raises(ProviderError):
            _parse_timeline(_wait_timeline_payload(_wait_timeline()))

    def test_envelope_size_limit_still_applies_with_timeline(self):
        """整体 64KiB envelope 限制继续生效。"""
        payload = _wait_timeline_payload(_wait_timeline())
        payload["projectName"] = "x" * (65 * 1024)
        with pytest.raises(ProviderError):
            _parse_timeline(payload)

    def test_observed_count_upper_bound(self):
        timeline = _wait_timeline(observedIntervalCount=4097)
        with pytest.raises(ProviderError):
            _parse_timeline(_wait_timeline_payload(timeline))


def labels_index(label: str, fields: list) -> int:
    for i, f in enumerate(fields):
        if f["label"] == label:
            return i
    raise ValueError(f"label {label} not found")


class TestOpenCodeProviderAdapterParseErrors:
    """各类非法输入的 ProviderError 映射。"""

    def _parse(self, payload=None, headers=None):
        if payload is None:
            payload = dict(_VALID_PAYLOAD)
        if headers is None:
            headers = dict(_HEADERS)
        return _make_adapter().parse(
            headers=headers, payload=payload, received_at=_received_at()
        )

    def test_header_missing(self):
        with pytest.raises(ProviderError) as e:
            self._parse(headers={})
        assert e.value.code == "invalid_payload"

    def test_body_event_missing(self):
        p = dict(_VALID_PAYLOAD)
        del p["event"]
        with pytest.raises(ProviderError) as e:
            self._parse(payload=p)
        assert e.value.code == "invalid_payload"

    def test_event_mismatch(self):
        with pytest.raises(ProviderError) as e:
            self._parse(
                headers={"x-opencode-event": "opencode.session_idle"},
                payload={**_VALID_PAYLOAD, "event": "opencode.session_error"},
            )
        assert e.value.code == "event_mismatch"

    def test_unsupported_event(self):
        with pytest.raises(ProviderError) as e:
            self._parse(
                headers={"x-opencode-event": "opencode.unknown"},
                payload={**_VALID_PAYLOAD, "event": "opencode.unknown"},
            )
        assert e.value.code == "unsupported_event"

    @pytest.mark.parametrize(
        "event_name", ["opencode.question_replied", "opencode.question_rejected"]
    )
    def test_question_completion_variants_rejected(self, event_name):
        with pytest.raises(ProviderError) as e:
            self._parse(
                headers={"x-opencode-event": event_name},
                payload={**_VALID_PAYLOAD, "event": event_name},
            )
        assert e.value.code == "unsupported_event"

    def test_unsupported_version_bool(self):
        with pytest.raises(ProviderError) as e:
            self._parse(payload={**_VALID_PAYLOAD, "version": True})
        assert e.value.code == "unsupported_version"

    def test_unsupported_version_wrong_int(self):
        with pytest.raises(ProviderError) as e:
            self._parse(payload={**_VALID_PAYLOAD, "version": 2})
        assert e.value.code == "unsupported_version"

    def test_missing_id(self):
        p = dict(_VALID_PAYLOAD)
        del p["id"]
        with pytest.raises(ProviderError) as e:
            self._parse(payload=p)
        assert e.value.code == "invalid_payload"

    def test_id_too_long(self):
        with pytest.raises(ProviderError):
            self._parse(payload={**_VALID_PAYLOAD, "id": "x" * 129})

    def test_missing_session_ref(self):
        p = {**_VALID_PAYLOAD, "session": {}}
        with pytest.raises(ProviderError):
            self._parse(payload=p)

    def test_unknown_top_field(self):
        with pytest.raises(ProviderError) as e:
            self._parse(payload={**_VALID_PAYLOAD, "extra_field": "bad"})
        assert e.value.code == "invalid_payload"

    def test_unknown_session_field(self):
        with pytest.raises(ProviderError):
            self._parse(
                payload={**_VALID_PAYLOAD, "session": {"ref": "x", "extra": "bad"}}
            )

    def test_sensitive_field(self):
        with pytest.raises(ProviderError):
            self._parse(payload={**_VALID_PAYLOAD, "cwd": "/home"})

    def test_session_idle_with_permission(self):
        with pytest.raises(ProviderError):
            self._parse(payload={**_VALID_PAYLOAD, "permission": {"category": "x"}})

    def test_session_idle_with_error(self):
        with pytest.raises(ProviderError):
            self._parse(payload={**_VALID_PAYLOAD, "error": {"category": "x"}})

    def test_session_error_missing_category(self):
        p = {**_VALID_PAYLOAD, "event": "opencode.session_error", "error": {}}
        with pytest.raises(ProviderError):
            self._parse(
                headers={"x-opencode-event": "opencode.session_error"}, payload=p
            )

    def test_session_error_with_permission(self):
        p = {
            **_VALID_PAYLOAD,
            "event": "opencode.session_error",
            "error": {"category": "x"},
            "permission": {"category": "y"},
        }
        with pytest.raises(ProviderError):
            self._parse(
                headers={"x-opencode-event": "opencode.session_error"}, payload=p
            )

    def test_permission_asked_missing_category(self):
        p = {**_VALID_PAYLOAD, "event": "opencode.permission_asked", "permission": {}}
        with pytest.raises(ProviderError):
            self._parse(
                headers={"x-opencode-event": "opencode.permission_asked"}, payload=p
            )

    def test_permission_asked_with_error(self):
        p = {
            **_VALID_PAYLOAD,
            "event": "opencode.permission_asked",
            "permission": {"category": "x"},
            "error": {"category": "y"},
        }
        with pytest.raises(ProviderError):
            self._parse(
                headers={"x-opencode-event": "opencode.permission_asked"}, payload=p
            )

    def test_task_duration_ms_from_duration_ms(self):
        """payload durationMs 应映射到 task_duration_ms。"""
        payload = {**_VALID_PAYLOAD, "durationMs": 15000}
        event = self._parse(payload=payload)
        assert event.task_duration_ms == 15000

    def test_task_duration_ms_none_when_missing(self):
        """durationMs 缺失时 task_duration_ms 为 None。"""
        event = self._parse(payload=dict(_VALID_PAYLOAD))
        assert event.task_duration_ms is None

    def test_task_duration_ms_not_in_to_dict(self):
        """task_duration_ms 不应出现在 to_dict() 输出中。"""
        payload = {**_VALID_PAYLOAD, "durationMs": 15000}
        event = self._parse(payload=payload)
        d = event.to_dict()
        assert "task_duration_ms" not in d

    def test_duration_ms_bool(self):
        with pytest.raises(ProviderError):
            self._parse(payload={**_VALID_PAYLOAD, "durationMs": True})

    def test_duration_ms_negative(self):
        with pytest.raises(ProviderError):
            self._parse(payload={**_VALID_PAYLOAD, "durationMs": -1})

    def test_duration_ms_too_large(self):
        with pytest.raises(ProviderError):
            self._parse(payload={**_VALID_PAYLOAD, "durationMs": 604800001})

    @pytest.mark.parametrize("value", [123, "", " " * 2, "x" * 129])
    def test_model_variant_requires_safe_bounded_string(self, value):
        with pytest.raises(ProviderError):
            self._parse(payload={**_VALID_PAYLOAD, "modelVariant": value})

    @pytest.mark.parametrize("value", [123, "not-a-timestamp", "2026-07-22T12:00:00"])
    def test_task_started_at_requires_timezone_timestamp(self, value):
        with pytest.raises(ProviderError) as exc:
            self._parse(payload={**_VALID_PAYLOAD, "taskStartedAt": value})
        assert exc.value.code == "invalid_payload"

    def test_task_started_at_is_allowlisted_and_normalized(self):
        event = self._parse(
            payload={
                **_VALID_PAYLOAD,
                "taskStartedAt": "2026-07-22T20:00:00+08:00",
            }
        )
        fields = {f["label"]: f["value"] for f in event.fields}
        assert fields["taskStartedAt"] == "2026-07-22T20:00:00+08:00"

    def test_emitted_at_no_tz(self):
        with pytest.raises(ProviderError):
            self._parse(payload={**_VALID_PAYLOAD, "emittedAt": "2026-07-22T12:00:00"})

    def test_error_message_does_not_leak_value(self):
        """确认错误消息不泄露原始字段值或 payload。"""
        try:
            self._parse(payload={**_VALID_PAYLOAD, "id": "x" * 129})
        except ProviderError as e:
            msg = str(e)
            assert "x" * 10 not in msg  # 不泄露原始值
            assert "id" not in msg or "无效" in msg  # 固定安全消息

    @pytest.mark.parametrize(
        "bad_field",
        [
            "cwd",
            "path",
            "username",
            "prompt",
            "messages",
            "assistant",
            "tool",
            "command",
            "diff",
            "url",
            "query",
            "token",
            "stack",
            "raw",
            "questions",
        ],
    )
    def test_sensitive_fields_rejected(self, bad_field):
        with pytest.raises(ProviderError):
            self._parse(payload={**_VALID_PAYLOAD, bad_field: "dummy"})

    def test_unknown_action_field_rejected(self):
        payload = {
            **_VALID_PAYLOAD,
            "event": "opencode.question_asked",
            "question": {"count": 1, "unknown": "must-not-pass"},
        }
        with pytest.raises(ProviderError):
            self._parse(
                payload=payload,
                headers={"x-opencode-event": "opencode.question_asked"},
            )

    def test_unknown_permission_field_rejected(self):
        payload = {
            **_VALID_PAYLOAD,
            "event": "opencode.permission_asked",
            "permission": {"category": "file_access", "raw": "no"},
        }
        with pytest.raises(ProviderError):
            self._parse(
                payload=payload,
                headers={"x-opencode-event": "opencode.permission_asked"},
            )

    @pytest.mark.parametrize(
        "permission",
        [
            {"count": 2, "items": []},
            {"count": 1, "items": [{"category": "x", "unknown": "bad"}]},
            {"count": 1, "items": [{"category": "x"}] * 17},
        ],
    )
    def test_invalid_aggregated_permission_rejected(self, permission):
        with pytest.raises(ProviderError):
            self._parse(
                payload={
                    **_VALID_PAYLOAD,
                    "event": "opencode.permission_asked",
                    "permission": permission,
                },
                headers={"x-opencode-event": "opencode.permission_asked"},
            )

    def test_oversized_payload_rejected(self):
        with pytest.raises(ProviderError):
            self._parse(payload={**_VALID_PAYLOAD, "projectName": "x" * (65 * 1024)})


# ─── Name 清洗集成 ────────────────────────────────────────


class TestOpenCodeNameCleaning:
    """验证 session.name 清洗和 ref12 回退。"""

    def _make(self, session_ref="sess_abc", session_name=None):
        p = dict(_VALID_PAYLOAD)
        p["session"] = {"ref": session_ref}
        if session_name is not None:
            p["session"]["name"] = session_name
        return _make_adapter().parse(
            headers=dict(_HEADERS), payload=p, received_at=_received_at()
        )

    def test_html_markdown_in_name(self):
        """HTML/MD 字符不清洗（依赖 renderer autoescape），但会保留。"""
        e = self._make(session_name="<b>Test</b>")
        assert "<b>Test</b>" in e.title

    def test_control_chars_normalized(self):
        e = self._make(session_name="line1\nline2\r\t")
        assert "line1" in e.title
        assert "\n" not in e.title

    def test_name_missing_fallback(self):
        e = self._make(session_name=None)
        assert e.title == "OpenCode Session sess_abc"

    def test_name_empty_fallback(self):
        e = self._make(session_name="  ")
        assert e.title == "OpenCode Session sess_abc"

    def test_ref12_special_chars(self):
        e = self._make(session_ref="a b!c.d", session_name=None)
        assert "a-b-c.d" in e.title

    def test_ref12_unicode_in_ref(self):
        e = self._make(session_ref="héllo-world", session_name=None)
        assert "héllo-world" not in e.title  # é 被替换

    def test_ref12_short(self):
        e = self._make(session_ref="ab", session_name=None)
        assert e.title == "OpenCode Session ab"

    def test_ref12_long(self):
        e = self._make(session_ref="abcdefghijklmnop", session_name=None)
        assert len(e.title.split()[-1]) == 12
        assert e.title == "OpenCode Session abcdefghijkl"

    def test_long_name(self):
        e = self._make(session_name="a" * 200)
        assert len(e.title) == 200


# ─── 安全日志 - 无 marker 泄漏 ────────────────────────────


class TestAgentModelValidation:
    """agent、model 可选但空/仅空白拒绝。"""

    def _parse(self, **overrides):
        p = dict(_VALID_PAYLOAD)
        p.update(overrides)
        return _make_adapter().parse(
            headers=dict(_HEADERS), payload=p, received_at=_received_at()
        )

    def test_agent_none(self):
        e = self._parse(agent=None)
        assert e.actor["name"] is None

    def test_agent_empty(self):
        with pytest.raises(ProviderError) as exc:
            self._parse(agent="")
        assert exc.value.code == "invalid_payload"

    def test_agent_blank(self):
        with pytest.raises(ProviderError) as exc:
            self._parse(agent="  ")
        assert exc.value.code == "invalid_payload"

    def test_agent_trimmed(self):
        e = self._parse(agent="  my-agent  ")
        assert e.actor["name"] == "my-agent"

    def test_model_too_long_after_trim(self):
        with pytest.raises(ProviderError):
            self._parse(model="x" * 129)

    def test_model_unicode(self):
        e = self._parse(model="gpt-5-中文")
        assert e.actor["name"] is None  # model not in actor
        labels = {f["label"] for f in e.fields}
        assert "model" in labels


class TestSessionRefInFields:
    """NormalizedEvent 中 sessionRef 字段使用 ref12，完整 ref 不出现。"""

    def _parse(self, ref="sess_long_ref_value_123456"):
        p = dict(_VALID_PAYLOAD)
        p["session"] = {"ref": ref}
        return _make_adapter().parse(
            headers=dict(_HEADERS), payload=p, received_at=_received_at()
        )

    def test_field_uses_ref12(self):
        e = self._parse("abcdefghijklmnop")
        fields = {f["label"]: f["value"] for f in e.fields}
        assert fields.get("sessionRef") == "abcdefghijkl"
        assert len(fields["sessionRef"]) == 12

    def test_full_ref_not_in_any_field(self):
        e = self._parse("my_long_secret_ref_value_123")
        full = "my_long_secret_ref_value_123"
        d = e.to_dict()
        text = str(d)
        assert full not in text
        assert "my-long-se" in text or "my_long_sec" in text

    def test_title_uses_ref12(self):
        e = self._parse("a b!c.d.e.f.g")
        assert "a-b-c-d-e-f" in e.title or "OpenCode Session" in e.title


class TestOpenCodeNoLeakage:
    def test_error_message_no_payload_value(self):
        """错误消息不含 payload 原始值。"""
        try:
            _make_adapter().parse(
                headers={"x-opencode-event": "opencode.session_idle"},
                payload={
                    "id": "evt1",
                    "event": "opencode.session_idle",
                    "version": 1,
                    "emittedAt": "invalid-date",
                    "session": {"ref": "ref1"},
                },
                received_at=_received_at(),
            )
        except ProviderError as e:
            msg = str(e)
            assert "invalid-date" not in msg
            assert "ref1" not in msg
            assert "evt1" not in msg


# ─── 注册与 ProviderRegistry 集成 ────────────────────────


class TestOpenCodeRegistration:
    def test_provider_key(self):
        assert _make_adapter().provider == "opencode"

    def test_register_in_registry_before_freeze(self):
        from core.providers import ProviderRegistry

        reg = ProviderRegistry()
        reg.register(_make_adapter())
        assert reg.get("opencode") is not None


# ─── raw 为空的验证 ───────────────────────────────────────


class TestOpenCodeRawEmpty:
    def test_raw_is_empty(self):
        e = _make_adapter().parse(
            headers=dict(_HEADERS),
            payload=dict(_VALID_PAYLOAD),
            received_at=_received_at(),
        )
        assert e.raw == {}
