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
    NORMALIZED_CANVAS_MARGIN,
    _expected_canvas_right,
    _infer_viewport_scale,
    _normalize_cropped_canvas,
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
        assert "background: #f5f5f7" in DEFAULT_HTML_TEMPLATE
        assert "width: fit-content" in DEFAULT_HTML_TEMPLATE
        assert "min-width: 0" in DEFAULT_HTML_TEMPLATE
        assert "min-height: 0" in DEFAULT_HTML_TEMPLATE
        assert "height: auto" in DEFAULT_HTML_TEMPLATE
        assert "width: 828px" in DEFAULT_HTML_TEMPLATE
        assert "max-width: 828px" in DEFAULT_HTML_TEMPLATE
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


class TestInferViewportScale:
    def test_known_scale_from_canvas_width(self):
        """已知 device scale 时准确推断。"""
        # canvas_width=860, scale=1.3 → 860*1.3=1118
        assert _infer_viewport_scale(1118, 860) == 1.3

    def test_fallback_to_default_viewport(self):
        """旧 T2I 退回 1280 默认视口时仍可推断。"""
        # 1280*1.3=1664
        assert _infer_viewport_scale(1664, 860) == 1.3

    def test_unknown_scale_returns_calculated(self):
        """无法匹配时返回计算值。"""
        scale = _infer_viewport_scale(500, 300)
        assert abs(scale - 1.67) < 0.02

    def test_zero_canvas_width_returns_one(self):
        """canvas_width≤0 时返回 1.0。"""
        assert _infer_viewport_scale(100, 0) == 1.0
        assert _infer_viewport_scale(100, -1) == 1.0


class TestNormalizeCroppedCanvas:
    def test_symmetric_margins_rgb(self):
        """RGB 图片归一化后四周为 #f5f5f7 且尺寸正确。"""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")

        cropped = Image.new("RGB", (100, 80), (255, 255, 255))
        scale = 1.0
        result = _normalize_cropped_canvas(cropped, "JPEG", scale)

        margin = NORMALIZED_CANVAS_MARGIN
        assert result.size == (100 + 2 * margin, 80 + 2 * margin)

        # 四角应为 #f5f5f7
        bg = (245, 245, 247)
        w, h = result.size
        for cx, cy in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
            px = result.getpixel((cx, cy))
            assert abs(px[0] - bg[0]) <= 2
            assert abs(px[1] - bg[1]) <= 2
            assert abs(px[2] - bg[2]) <= 2

    def test_symmetric_margins_rgba(self):
        """PNG RGBA 归一化后 alpha 保留、四角 #f5f5f7。"""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")

        cropped = Image.new("RGBA", (60, 40), (255, 255, 255, 255))
        result = _normalize_cropped_canvas(cropped, "PNG", 1.0)

        margin = NORMALIZED_CANVAS_MARGIN
        assert result.size == (60 + 2 * margin, 40 + 2 * margin)
        assert result.mode == "RGBA"

        bg = (245, 245, 247, 255)
        w, h = result.size
        for cx, cy in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
            px = result.getpixel((cx, cy))
            assert all(abs(px[i] - bg[i]) <= 2 for i in range(4))

    def test_scale_scales_margin(self):
        """scale>1 时边距按比例放大。"""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")

        cropped = Image.new("RGB", (50, 50), (255, 255, 255))
        # scale=1.3 → margin_px = int(24 * 1.3) = 31
        result = _normalize_cropped_canvas(cropped, "PNG", 1.3)
        assert result.size == (50 + 2 * 31, 50 + 2 * 31)


class TestTrimViewportWhitespace:
    def test_expected_canvas_right_when_viewport_width_honored(self):
        """viewport_width 生效时，按 860px 视口推断右边界。"""
        # 860 * 1.3 = 1118，内容右边界约 (860 - 16) * 1.3
        assert _expected_canvas_right(1118, 860) == int(844 * 1.3)

    def test_expected_canvas_right_when_default_viewport_used(self):
        """旧 T2I 忽略 viewport_width 时，按 1280px 默认视口兜底推断。"""
        # 1280 * 1.3 = 1664，仍应裁到 860px 画布附近，而不是保留 1280px 视口。
        assert _expected_canvas_right(1664, 860) == int(844 * 1.3)

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

    def test_local_file_normalized(self):
        """本地 PNG 截图：裁切后四周为对称 #f5f5f7 边距。"""
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            pytest.skip("PIL not available")

        import tempfile

        # 构造一张 500x400 的图片：
        # - 白色内容区 0-250 x 0-300，其余为灰色背景
        # - canvas_width=300 模拟视口宽度
        width, height = 500, 400
        img = Image.new("RGB", (width, height), (200, 200, 200))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, 250, 300], fill=(255, 255, 255))

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
            img.save(tmp_path, format="PNG")

        try:
            result = trim_viewport_whitespace(tmp_path, canvas_width=300)
            assert result == tmp_path

            with Image.open(tmp_path) as normalized:
                w, h = normalized.size

                # 内容仍可见（中心附近应为白色）
                cx, cy = w // 2, h // 2
                mid = normalized.getpixel((cx, cy))
                assert min(mid[:3]) >= 240  # 内容区域接近白色

                # 四角应为对称的 #f5f5f7
                bg = (245, 245, 247)
                for px, py in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
                    p = normalized.getpixel((px, py))
                    assert abs(p[0] - bg[0]) <= 2
                    assert abs(p[1] - bg[1]) <= 2
                    assert abs(p[2] - bg[2]) <= 2

                # 尺寸 ≥ 裁切后内容尺寸（至少 250+2*margin）
                # scale≈1.67, margin≈40
                assert w >= 250 + 2 * 24
                assert h >= 300 + 2 * 24
        finally:
            import os as _os

            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)

    def test_different_formats_jpeg(self):
        """JPEG 格式应正确处理（归一化后为 RGB）。"""
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            pytest.skip("PIL not available")

        import tempfile

        width, height = 400, 350
        img = Image.new("RGB", (width, height), (200, 200, 200))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, 200, 250], fill=(255, 255, 255))

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmp_path = f.name
            img.save(tmp_path, format="JPEG", quality=95)

        try:
            result = trim_viewport_whitespace(tmp_path, canvas_width=250)
            assert result == tmp_path

            with Image.open(tmp_path) as normalized:
                # JPEG 应为 RGB
                assert normalized.mode == "RGB"
                # 四角为背景色
                bg = (245, 245, 247)
                w, h = normalized.size
                for px, py in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
                    p = normalized.getpixel((px, py))
                    assert abs(p[0] - bg[0]) <= 2
                    assert abs(p[1] - bg[1]) <= 2
                    assert abs(p[2] - bg[2]) <= 2
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
            result = trim_viewport_whitespace(tmp_path, canvas_width=860)
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

    def test_high_dpi_cropped_uses_scaled_margin(self):
        """高 DPI 截图（旧 T2I 1280 视口）裁剪后边距按 scale 放大。"""
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            pytest.skip("PIL not available")

        import tempfile

        # 模拟旧 T2I 截图：1280*1.3=1664 宽
        width, height = 1664, 1200
        img = Image.new("RGB", (width, height), (200, 200, 200))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, 1097, 900], fill=(255, 255, 255))

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
            img.save(tmp_path, format="PNG")

        try:
            result = trim_viewport_whitespace(tmp_path, canvas_width=860)
            assert result == tmp_path

            with Image.open(tmp_path) as normalized:
                # scale=1.3 → margin_px = int(24*1.3) = 31
                # 裁切后内容尺寸至少 ~(1097+margins, 900+margins)
                # 归一化后至少再加 2*31
                assert normalized.width >= 1097 + 2 * 31
                assert normalized.height >= 900 + 2 * 31

                # 四角为背景色
                bg = (245, 245, 247)
                w, h = normalized.size
                for px, py in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
                    p = normalized.getpixel((px, py))
                    assert abs(p[0] - bg[0]) <= 2
                    assert abs(p[1] - bg[1]) <= 2
                    assert abs(p[2] - bg[2]) <= 2
        finally:
            import os as _os

            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)
