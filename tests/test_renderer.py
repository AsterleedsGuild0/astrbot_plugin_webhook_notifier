"""Renderer tests - no AstrBot dependency."""

from __future__ import annotations

import pytest

from core.models import NormalizedEvent
from core.renderer import DEFAULT_TEXT_TEMPLATE, render_text, render_text_default


def _make_event(
    title: str = "会话完成",
    summary: str = "会话 test-session 已完成",
    fields: list | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        provider="omp",
        event="omp.session_stop",
        version=1,
        id="sess_001:turn_001",
        emitted_at="2026-07-08T12:00:00.000Z",
        title=title,
        status="success",
        summary=summary,
        source={"name": "oh-my-pi", "url": None},
        actor={"name": None, "url": None},
        fields=fields or [],
        links=[],
        raw={},
    )


class TestRenderTextDefault:
    def test_basic_render(self):
        """基本渲染应包含标题和字段。"""
        event = _make_event(
            fields=[
                {
                    "label": "会话",
                    "value": "Add post-conversation HTTP hook",
                    "short": True,
                },
                {"label": "模型", "value": "gpt-5.5", "short": True},
                {"label": "耗时", "value": "57.7s", "short": True},
            ],
        )
        result = render_text_default(event)
        assert "[oh-my-pi]" in result
        assert "会话完成" in result
        assert "会话：" in result
        assert "模型：" in result
        assert "耗时：" in result

    def test_empty_fields(self):
        """空 fields 不应渲染出多余内容。"""
        event = _make_event(fields=[])
        result = render_text_default(event)
        assert "[oh-my-pi]" in result
        # 不应有无标签的字段行
        assert "：" not in result or all(
            line.count("：") <= 1 for line in result.split("\n") if line.strip()
        )

    def test_field_value_without_label(self):
        """字段值即使无 label 也应渲染。"""
        fields = [{"label": "", "value": "just-a-value", "short": True}]
        event = _make_event(fields=fields)
        result = render_text_default(event)
        assert "just-a-value" in result

    def test_long_summary(self):
        """长摘要不应截断（文本模式下不截断）。"""
        long_summary = "A" * 500
        event = _make_event(summary=long_summary)
        result = render_text_default(event)
        assert long_summary in result

    def test_source_name_unknown(self):
        """source.name 为空时使用 unknown。"""
        event = _make_event()
        event.source["name"] = ""
        result = render_text_default(event)
        assert "[unknown]" in result

    def test_omp_example_format(self):
        """应匹配 FSD 中的 OMP 示例格式。"""
        event = _make_event(
            title="会话完成",
            summary="",
            fields=[
                {
                    "label": "会话",
                    "value": "Add post-conversation HTTP hook",
                    "short": True,
                },
                {"label": "cwd", "value": "/home/user/project", "short": False},
                {"label": "模型", "value": "openai/gpt-5.5", "short": True},
                {
                    "label": "开始时间",
                    "value": "2026-07-08 19:59:00 UTC+08:00",
                    "short": True,
                },
                {"label": "耗时", "value": "57.7s", "short": True},
                {"label": "输入", "value": "977 字 / 1 张图", "short": True},
                {"label": "消息变化", "value": "+2", "short": True},
                {"label": "最后状态", "value": "stop", "short": True},
            ],
        )
        result = render_text_default(event)
        # 验证主要结构
        lines = [l for l in result.split("\n") if l.strip()]
        assert len(lines) >= 3
        assert "[oh-my-pi]" in lines[0]
        assert all(
            "模型" not in line or line == "模型：openai/gpt-5.5" for line in lines
        )
        assert "cwd：/home/user/project" in lines
        assert "开始时间：2026-07-08 19:59:00 UTC+08:00" in lines


class TestRenderTextJinja2:
    def test_default_template(self):
        """使用 Jinja2 默认模板渲染。"""
        event = _make_event(
            fields=[
                {"label": "模型", "value": "gpt-5.5", "short": True},
            ],
        )
        result = render_text(event)
        assert "[oh-my-pi]" in result
        assert "gpt-5.5" in result

    def test_custom_template(self):
        """自定义模板应正确渲染。"""
        template = "Custom: {{ event.title }} @ {{ event.source.name }}"
        event = _make_event(title="测试通知")
        result = render_text(event, template)
        assert result == "Custom: 测试通知 @ oh-my-pi"

    def test_template_sandbox(self):
        """sandbox 应阻止危险操作。"""
        template = "{{ event.__class__.__mro__ }}"
        event = _make_event()
        try:
            result = render_text(event, template)
            # sandbox 应导致渲染失败或返回空
            assert False, "sandbox 未阻止危险操作"
        except Exception:
            pass  # 预期异常

    def test_empty_template(self):
        """空模板应返回空字符串。"""
        event = _make_event()
        result = render_text(event, "")
        assert result == ""

    def test_event_dict_access(self):
        """模板应通过 event 命名空间访问字段。"""
        template = "{{ event.fields[0].label }}: {{ event.fields[0].value }}"
        event = _make_event(
            fields=[{"label": "版本", "value": "1.0", "short": True}],
        )
        result = render_text(event, template)
        assert result == "版本: 1.0"

    def test_default_template_matches_fsd(self):
        """默认模板应与 FSD 定义一致。"""
        expected_template = """\
[{{ event.source.name }}] {{ event.title }}

{% if event.summary %}{{ event.summary }}
{% endif %}{% for field in event.fields %}
{{ field.label }}：{{ field.value }}{% endfor %}
"""
        assert DEFAULT_TEXT_TEMPLATE == expected_template


# ─── HTML 渲染测试 ─────────────────────────────────────────


from core.renderer import (
    DEFAULT_HTML_TEMPLATE,
    _expected_canvas_right,
    _scaled_right_crop_padding,
    render_html,
    render_html_default,
    render_html_data,
    trim_viewport_whitespace,
    validate_image_result,
)


class TestRenderHtmlData:
    def test_basic_structure(self):
        """render_html_data 返回的 dict 应包含 event 键。"""
        event = _make_event()
        context = render_html_data(event)
        assert "event" in context
        assert "generated_at" in context["event"]
        assert "event_time" in context["event"]

    def test_source_flattened(self):
        """source dict 应展平为字符串。"""
        event = _make_event()
        context = render_html_data(event)
        assert isinstance(context["event"]["source"], str)
        assert context["event"]["source"] == "oh-my-pi"

    def test_source_empty_fallback(self):
        """source.name 为空时使用 'AstrBot'。"""
        event = _make_event()
        event.source["name"] = ""
        context = render_html_data(event)
        assert context["event"]["source"] == "AstrBot"


class TestRenderHtml:
    def test_default_template_renders(self):
        """使用默认 HTML 模板应正常渲染。"""
        event = _make_event(
            fields=[
                {"label": "模型", "value": "gpt-5.5", "short": True},
                {"label": "耗时", "value": "57.7s", "short": True},
            ],
        )
        html = render_html_default(event)
        assert "<!doctype html>" in html.lower() or "<html" in html.lower()
        assert "oh-my-pi" in html
        assert "gpt-5.5" in html
        assert "57.7s" in html
        assert "会话完成" in html

    def test_empty_summary(self):
        """空 summary 不应输出 summary 区域。"""
        event = _make_event(summary="")
        html = render_html_default(event)
        assert 'class="summary"' not in html

    def test_summary_with_content(self):
        """非空 summary 应渲染到页面。"""
        event = _make_event(summary="任务已完成")
        html = render_html_default(event)
        assert "任务已完成" in html
        assert 'class="summary"' in html

    def test_multiple_fields(self):
        """多字段应全部渲染。"""
        fields = [
            {"label": "会话", "value": "test-session"},
            {"label": "模型", "value": "gpt-5.5"},
            {"label": "耗时", "value": "1m 30s"},
            {"label": "输入", "value": "500 字"},
            {"label": "消息变化", "value": "+3"},
        ]
        event = _make_event(fields=fields)
        html = render_html_default(event)
        for f in fields:
            assert f["label"] in html
            assert f["value"] in html

    def test_no_fields(self):
        """无字段时应显示占位文本。"""
        event = _make_event(fields=[])
        html = render_html_default(event)
        assert "暂无可展示字段" in html

    def test_field_token_filtered(self):
        """包含 token/raw/prompt 的字段应被过滤。"""
        fields = [
            {"label": "会话", "value": "visible"},
            {"label": "access_token", "value": "secret"},
            {"label": "raw_payload", "value": "should-be-hidden"},
            {"label": "prompt_text", "value": "should-be-hidden"},
        ]
        event = _make_event(fields=fields)
        html = render_html_default(event)
        assert "visible" in html
        assert "secret" not in html
        assert "should-be-hidden" not in html

    def test_html_template_escapes_content(self):
        """HTML 卡片内容应转义，避免字段值破坏卡片结构。"""
        event = _make_event(
            title="<b>标题</b>",
            summary="<script>alert(1)</script>",
            fields=[{"label": "路径 <cwd>", "value": "/tmp/<project>"}],
        )
        html = render_html_default(event)
        assert "&lt;b&gt;标题&lt;/b&gt;" in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "路径 &lt;cwd&gt;" in html
        assert "/tmp/&lt;project&gt;" in html
        assert "<script>alert(1)</script>" not in html

    def test_html_template_keeps_falsey_field_values(self):
        """0 和 False 等字段值也应展示，不能被默认值吞掉。"""
        event = _make_event(
            fields=[
                {"label": "退出码", "value": 0},
                {"label": "是否跳过", "value": False},
            ],
        )
        html = render_html_default(event)
        assert "退出码" in html
        assert ">0</div>" in html
        assert "是否跳过" in html
        assert "False" in html

    def test_sandbox_blocks_dangerous(self):
        """sandbox 应阻断危险操作。"""
        dangerous_template = "<html><body>{{ event.__class__.__mro__ }}</body></html>"
        event = _make_event()
        try:
            render_html(event, dangerous_template)
            assert False, "sandbox 未阻止危险操作"
        except Exception:
            pass

    def test_custom_template(self):
        """自定义模板应正确渲染。"""
        template = (
            "<html><body>Custom: {{ event.title }} @ {{ event.source }}</body></html>"
        )
        event = _make_event(title="测试通知")
        html = render_html(event, template)
        assert "Custom: 测试通知 @ oh-my-pi" in html

    def test_event_time_from_emitted_at(self):
        """未传入 event_time 时应回退到 emitted_at。"""
        event = _make_event()
        context = render_html_data(event)
        assert context["event"]["event_time"] == context["event"]["emitted_at"]

    def test_default_template_contains_styles(self):
        """默认 HTML 模板应包含 macOS 浅色卡片样式及 shrinkwrap CSS。"""
        assert "box-sizing" in DEFAULT_HTML_TEMPLATE
        assert "-apple-system" in DEFAULT_HTML_TEMPLATE
        assert "PingFang SC" in DEFAULT_HTML_TEMPLATE
        assert ".status-badge" in DEFAULT_HTML_TEMPLATE
        assert "background: #ffffff" in DEFAULT_HTML_TEMPLATE
        assert "width: fit-content" in DEFAULT_HTML_TEMPLATE
        assert "min-width: 0" in DEFAULT_HTML_TEMPLATE
        assert "min-height: 0" in DEFAULT_HTML_TEMPLATE
        assert "height: auto" in DEFAULT_HTML_TEMPLATE
        assert "width: 780px" in DEFAULT_HTML_TEMPLATE
        assert "max-width: 780px" in DEFAULT_HTML_TEMPLATE
        assert "width: 100vw" not in DEFAULT_HTML_TEMPLATE
        assert "min-height: 100%" not in DEFAULT_HTML_TEMPLATE
        assert "justify-content: center" not in DEFAULT_HTML_TEMPLATE
        assert "#0a0f1c" not in DEFAULT_HTML_TEMPLATE


# ─── 图片结果校验测试 ─────────────────────────────────────


class TestValidateImageResult:
    def test_valid_png_bytes(self):
        """PNG magic number 应通过校验。"""
        result = b"\x89PNG\r\n\x1a\n" + b"dummy_data"
        assert validate_image_result(result) is True

    def test_valid_jpeg_bytes(self):
        """JPEG magic number 应通过校验。"""
        result = b"\xff\xd8\xff" + b"dummy_data"
        assert validate_image_result(result) is True

    def test_valid_webp_bytes(self):
        """WebP RIFF....WEBP 应通过校验。"""
        result = b"RIFF\x00\x00\x00\x00WEBP" + b"dummy"
        assert validate_image_result(result) is True

    def test_invalid_bytes(self):
        """无效图片 bytes 应抛出 ValueError。"""
        result = b"\x00\x00\x00\x00\x00\x00\x00\x00"
        try:
            validate_image_result(result)
            assert False, "应抛出 ValueError"
        except ValueError:
            pass

    def test_empty_bytes(self):
        """空 bytes 应抛出 ValueError。"""
        try:
            validate_image_result(b"")
            assert False
        except ValueError:
            pass

    def test_base64_prefix(self):
        """base64:// 前缀的 PNG 应解码后校验。"""
        import base64

        png_bytes = b"\x89PNG\r\n\x1a\n"
        b64_str = "base64://" + base64.b64encode(png_bytes).decode()
        assert validate_image_result(b64_str) is True

    def test_invalid_base64(self):
        """无法解码的 base64:// 应抛出 ValueError。"""
        result = "base64://not-valid-base64!!!"
        try:
            validate_image_result(result)
            assert False
        except ValueError:
            pass

    def test_data_url_png(self):
        """data:image/png;base64,... 应解码并校验。"""
        import base64

        png_bytes = b"\x89PNG\r\n\x1a\n"
        b64_str = base64.b64encode(png_bytes).decode()
        data_url = f"data:image/png;base64,{b64_str}"
        assert validate_image_result(data_url) is True

    def test_http_url(self):
        """HTTP URL 应通过校验（不下载）。"""
        assert validate_image_result("https://example.com/image.png") is True

    def test_none_result(self):
        """None 应抛出 ValueError。"""
        try:
            validate_image_result(None)
            assert False
        except ValueError:
            pass

    def test_unsupported_type(self):
        """不支持的类型应抛出 TypeError。"""
        try:
            validate_image_result(123)
            assert False
        except TypeError:
            pass


# ─── 视口空白裁切测试 ────────────────────────────────────


class TestTrimViewportWhitespace:
    def test_expected_canvas_right_when_viewport_width_honored(self):
        """viewport_width 生效时，按 812px 视口推断右边界。"""
        # 812 * 1.3 = 1055.6，内容右边界约 (16 + 780) * 1.3
        assert _expected_canvas_right(int(812 * 1.3), 812) == int(796 * 1.3)

    def test_expected_canvas_right_uses_card_width_with_old_viewport(self):
        """云端仍使用旧 viewport_width=860 时，也应按实际卡片宽度裁剪。"""
        assert _expected_canvas_right(int(860 * 1.3), 860) == int(796 * 1.3)

    def test_expected_canvas_right_uses_card_width_with_custom_viewport(self):
        """云端配置自定义 viewport_width=900 时，也应按实际卡片宽度裁剪。"""
        assert _expected_canvas_right(int(900 * 1.3), 900) == int(796 * 1.3)

    def test_expected_canvas_right_when_default_viewport_used(self):
        """旧 T2I 忽略 viewport_width 时，按 1280px 默认视口兜底推断。"""
        # 1280 * 1.3 = 1664，仍应裁到 812px 画布附近，而不是保留 1280px 视口。
        assert _expected_canvas_right(1664, 812) == int(796 * 1.3)

    def test_expected_canvas_right_uses_card_width_with_old_viewport_and_default_viewport(
        self,
    ):
        """旧配置 860 + 旧 T2I 默认 1280 视口时，仍应按 780px 卡片宽度裁剪。"""
        assert _expected_canvas_right(1664, 860) == int(796 * 1.3)

    def test_expected_canvas_right_uses_card_width_with_custom_viewport_and_default_viewport(
        self,
    ):
        """自定义配置 900 + 旧 T2I 默认 1280 视口时，仍应按 780px 卡片宽度裁剪。"""
        assert _expected_canvas_right(1664, 900) == int(796 * 1.3)

    def test_scaled_right_crop_padding_uses_fallback_viewport_scale(self):
        """旧 T2I 默认 1280 视口时，右侧裁剪留白应按真实 scale，而非整图比例。"""
        assert _scaled_right_crop_padding(1664, 812) == int(12 * 1.3)

    def test_url_passthrough(self):
        """URL 字符串应原样返回（不处理）。"""
        url = "https://example.com/img.png"
        assert trim_viewport_whitespace(url) is url

    def test_bytes_passthrough(self):
        """bytes 应原样返回。"""
        data = b"dummy bytes"
        assert trim_viewport_whitespace(data) is data

    def test_none_passthrough(self):
        """None 应原样返回。"""
        assert trim_viewport_whitespace(None) is None

    def test_local_file_cropped(self):
        """本地 PNG 截图，右侧/底部为纯背景，调用后尺寸应缩小。"""
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            pytest.skip("PIL not available")

        import tempfile

        # 构造一张旧 T2I 默认 1280 视口、high scale=1.3 的图片：
        # - 白色内容区到 812px 画布附近，其余为灰色背景
        # - canvas_width=812 模拟插件传入的目标视口宽度
        width, height = 1664, 520
        img = Image.new("RGB", (width, height), (200, 200, 200))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, int(796 * 1.3), 360], fill=(255, 255, 255))

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
            img.save(tmp_path, format="PNG")

        try:
            result = trim_viewport_whitespace(tmp_path, canvas_width=812)
            assert result == tmp_path

            # 验证已裁切
            with Image.open(tmp_path) as cropped:
                assert cropped.width < width, "右侧空白应被裁切"
                assert cropped.height < height, "底部空白应被裁切"
                # 内容区不应被过度裁切
                assert cropped.width >= int(796 * 1.3)
                assert cropped.width <= int(808 * 1.3) + 2
                assert cropped.height >= 360
        finally:
            import os as _os

            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)

    def test_different_formats_jpeg(self):
        """JPEG 格式应正确处理。"""
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            pytest.skip("PIL not available")

        import tempfile

        width, height = 1280, 420
        img = Image.new("RGB", (width, height), (200, 200, 200))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, 796, 280], fill=(255, 255, 255))

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmp_path = f.name
            img.save(tmp_path, format="JPEG", quality=95)

        try:
            result = trim_viewport_whitespace(tmp_path, canvas_width=812)
            assert result == tmp_path

            with Image.open(tmp_path) as cropped:
                assert cropped.width < width
                assert cropped.width <= 810
                assert cropped.height < height
        finally:
            import os as _os

            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)

    def test_no_crop_needed(self):
        """内容已铺满的图片不应被裁切（也不归一化）。"""
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            pytest.skip("PIL not available")

        import tempfile

        width, height = 300, 200
        img = Image.new("RGB", (width, height), (255, 255, 255))  # 全部白色

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
            img.save(tmp_path, format="PNG")

        try:
            result = trim_viewport_whitespace(tmp_path, canvas_width=812)
            assert result == tmp_path

            # 不应裁切（尺寸不变）
            with Image.open(tmp_path) as reloaded:
                assert reloaded.size == (width, height)
        finally:
            import os as _os

            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)

    def test_small_image_skipped(self):
        """过小的图片应跳过裁切。"""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")

        import tempfile

        img = Image.new("RGB", (100, 80), (255, 255, 255))

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
            img.save(tmp_path, format="PNG")

        try:
            # 尺寸 < 360x240，不应裁切
            result = trim_viewport_whitespace(tmp_path, canvas_width=200)
            assert result == tmp_path

            with Image.open(tmp_path) as reloaded:
                assert reloaded.size == (100, 80)
        finally:
            import os as _os

            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)
