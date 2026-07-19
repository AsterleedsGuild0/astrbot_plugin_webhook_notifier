from __future__ import annotations

import importlib.util
import json
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
PAGE_DIR = ROOT / "pages" / "template-editor"
# AstrBot market limit: https://github.com/AstrBotDevs/AstrBot/wiki/en-dev-star-plugin-publish
SIZE_LIMIT = 16 * 1024 * 1024


class ResourceReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []
        self.iframes: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        resource_attributes = {
            "script": {"src"},
            "link": {"href"},
            "img": {"src", "srcset"},
            "source": {"src", "srcset"},
            "video": {"src", "poster"},
        }
        expected = resource_attributes.get(tag, set())
        self.references.extend(
            value for name, value in attrs if name in expected and value is not None
        )
        if tag == "iframe":
            self.iframes.append(dict(attrs))


def load_package_module():
    spec = importlib.util.spec_from_file_location(
        "package_plugin", ROOT / "scripts" / "package_plugin.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def built_files() -> list[Path]:
    return [path for path in PAGE_DIR.rglob("*") if path.is_file()]


def test_built_page_contains_assets_and_monaco_notices() -> None:
    assert (PAGE_DIR / "index.html").is_file()
    assert any((PAGE_DIR / "assets").glob("*.js"))
    assert any((PAGE_DIR / "assets").glob("*.css"))
    assert (PAGE_DIR / "vendor" / "monaco" / "LICENSE").is_file()
    assert (PAGE_DIR / "vendor" / "monaco" / "ThirdPartyNotices.txt").is_file()


def test_built_page_keeps_non_blocking_monaco_worker_diagnostics() -> None:
    files = built_files()
    payload = b"\n".join(path.read_bytes() for path in files if path.suffix == ".js")
    lowered = payload.lower()

    assert not any(path.suffix == ".map" for path in files)
    assert b"__whn_monaco_phase0__" in lowered
    assert b"workersverified" in lowered
    assert b"workerdiagnosticscount" in lowered
    assert b"workermessages" in lowered
    assert b"workererrors" in lowered
    assert b"unhandledrejection" in lowered
    assert all(label in lowered for label in (b"html", b"css", b"json"))
    assert b"editor.action.formatdocument" in lowered
    assert b"issupported" in lowered
    assert b"aria-hidden" in lowered


def test_frontend_source_uses_isolated_html_worker_diagnostic_editor() -> None:
    source = (ROOT / "frontend" / "src" / "monaco.js").read_text(encoding="utf-8")
    diagnostic_source = source[
        source.index("export async function runWorkerDiagnostics") :
    ]

    assert "document.createElement('div')" in diagnostic_source
    assert "setAttribute('aria-hidden', 'true')" in diagnostic_source
    assert "style.pointerEvents = 'none'" in diagnostic_source
    assert "style.width = '1px'" in diagnostic_source
    assert "style.height = '1px'" in diagnostic_source
    assert "monaco.editor.create(diagnosticHost" in diagnostic_source
    assert "model: models[0]" in diagnostic_source
    assert "getAction('editor.action.formatDocument')" in diagnostic_source
    assert "formatAction.isSupported()" in diagnostic_source
    assert diagnostic_source.count("formatAction.run()") == 1
    assert "diagnosticEditor?.dispose()" in diagnostic_source
    assert "models.forEach((model) => model.dispose())" in diagnostic_source
    assert "diagnosticHost?.remove()" in diagnostic_source
    assert "templateEditor" not in diagnostic_source


def test_built_page_contains_phase1_bridge_contract_and_management_ui() -> None:
    scripts = list((PAGE_DIR / "assets").glob("*.js"))
    payload = b"\n".join(path.read_bytes() for path in scripts)
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")

    endpoints = (
        b"base-url",
        b"templates",
        b"templates/save",
        b"templates/apply",
        b"templates/delete",
        b"templates/preview",
    )
    assert all(endpoint in payload for endpoint in endpoints)
    assert b"apiGet" in payload and b"apiPost" in payload
    assert b"beforeunload" in payload
    assert b"expected_revision" in payload
    assert b"canvas_width" in payload
    assert b"500" in payload
    assert "通知模板管理" in html
    assert "template-list" in html
    assert "save-apply-button" in html
    assert "preview-data-panel" in html
    assert "Webhook Base URL" in html
    assert "copy-base-url-button" in html
    assert "Endpoint Path" in html


def test_base_url_ui_uses_minimum_bridge_contract_without_sensitive_management() -> (
    None
):
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    api_source = (ROOT / "frontend" / "src" / "api.js").read_text(encoding="utf-8")
    main_source = (ROOT / "frontend" / "src" / "main.js").read_text(encoding="utf-8")
    frontend_source = "\n".join((html, api_source, main_source)).lower()

    assert "baseUrl: 'base-url'" in api_source
    assert "getBaseUrl()" in api_source
    assert "bridge.apiGet(ENDPOINTS.baseUrl)" in api_source
    assert 'id="base-url-value"' in html
    assert 'id="copy-base-url-button"' in html
    assert 'id="base-url-warning"' in html
    assert "当前为本地监听地址" in html
    assert "自行将此 Base URL 与 Endpoint Path 组合" in html
    assert "navigator.clipboard" in main_source
    assert "document.execCommand('copy')" in main_source
    assert "baseUrl = result.base_url" in main_source
    assert "value.textContent = ''" in main_source
    assert "showNotice('已复制')" in main_source
    assert "console." not in frontend_source
    assert "/webhook" not in frontend_source
    assert 'type="url"' not in html
    assert "localstorage" not in frontend_source
    assert "sessionstorage" not in frontend_source
    assert "indexeddb" not in frontend_source

    forbidden_management_terms = (
        "server_secret",
        "endpoint-list",
        "endpoint-input",
        "token-input",
        "token-list",
        "token",
        "registry",
        "owner",
        "umo",
    )
    assert all(term not in frontend_source for term in forbidden_management_terms)


def test_built_base_url_ui_is_synced_without_embedding_sensitive_fields() -> None:
    scripts = list((PAGE_DIR / "assets").glob("*.js"))
    payload = b"\n".join(path.read_bytes() for path in scripts).lower()
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    built_source = html.lower().encode() + b"\n" + payload

    assert b"base-url" in payload
    assert b"navigator.clipboard" in payload
    assert "Webhook Base URL" in html
    assert "copy-base-url-button" in html
    assert "当前为本地监听地址" in html
    assert b"server_secret" not in built_source
    assert b"endpoint-list" not in built_source
    assert b"token-input" not in built_source


def test_preview_uses_responsive_scale_to_fit_without_iframe_dom_access() -> None:
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "frontend" / "src" / "main.js").read_text(encoding="utf-8")
    styles = (ROOT / "frontend" / "src" / "style.css").read_text(encoding="utf-8")

    assert 'id="preview-canvas"' in html
    assert 'data-preview-layout="scale-to-fit"' in html
    assert "function recalculatePreviewScale" in source
    assert "ResizeObserver" in source
    assert "clientWidth" in source and "clientHeight" in source
    assert "requestAnimationFrame(recalculatePreviewScale)" in source
    assert "style.transform = `scale(${scale})`" in source
    assert "contentDocument" not in source
    assert "contentWindow" not in source

    preview_stage = re.search(
        r"\.preview-stage\s*\{(?P<body>.*?)\n\}", styles, re.DOTALL
    )
    assert preview_stage
    assert "overflow: hidden" in preview_stage.group("body")
    assert ".preview-canvas" in styles


def test_metadata_status_never_collapses_into_vertical_text() -> None:
    styles = (ROOT / "frontend" / "src" / "style.css").read_text(encoding="utf-8")
    for selector in (".document-state", ".dirty-indicator", ".revision-label"):
        rule = re.search(
            rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\n\}}", styles, re.DOTALL
        )
        assert rule
        assert "white-space: nowrap" in rule.group("body")


def test_delete_flow_explains_constraints_and_switches_active_template_first() -> None:
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "frontend" / "src" / "main.js").read_text(encoding="utf-8")
    delete_flow = source[
        source.index("async function deleteCurrent") : source.index(
            "function showFieldError"
        )
    ]

    assert 'aria-describedby="delete-template-help"' in html
    assert "内置模板不可删除" in source
    assert "放弃草稿" in source
    assert "先应用内置模板，再删除当前模板" in source
    assert "已切换到内置模板，但删除失败" in source
    assert "未找到 built-in 内置模板" in source
    assert delete_flow.count("confirmAction(") == 1
    assert "当前未保存修改也会被放弃" in delete_flow
    assert re.search(
        r"api\.applyTemplate\(\{\s*id:\s*'built-in',\s*expected_revision:\s*0\s*\}\)",
        delete_flow,
    )
    assert delete_flow.index("api.applyTemplate") < delete_flow.index(
        "api.deleteTemplate"
    )


def test_frontend_sources_use_bridge_for_crud_and_include_dirty_guard() -> None:
    sources = [
        path for path in (ROOT / "frontend" / "src").rglob("*.js") if path.is_file()
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in sources)

    assert "window.AstrBotPluginPage" in source
    assert "bridge.apiGet" in source
    assert "bridge.apiPost" in source
    assert "beforeunload" in source
    assert "isDirty" in source
    assert "confirmAction" in source
    assert "window.confirm" not in source
    assert "必须从AstrBot插件详情页打开" in source
    assert "fetch(" not in source
    assert "postMessage(" not in source


def test_in_page_confirmation_dialog_replaces_sandboxed_native_confirm() -> None:
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "frontend" / "src" / "confirm.js").read_text(encoding="utf-8")
    scripts = list((PAGE_DIR / "assets").glob("*.js"))
    payload = b"\n".join(path.read_bytes() for path in scripts)

    assert 'id="confirm-overlay"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-labelledby="confirm-title"' in html
    assert 'aria-describedby="confirm-message"' in html
    assert "return new Promise" in source
    assert "event.key === 'Escape'" in source
    assert "event.target === overlay" in source
    assert "cancelButton.addEventListener('click'" in source
    assert "confirmButton.addEventListener('click'" in source
    assert "previousFocus = document.activeElement" in source
    assert "cancelButton.focus()" in source
    assert "focusTarget.focus()" in source
    assert b"window.confirm" not in payload


def test_page_resource_references_are_local() -> None:
    parser = ResourceReferenceParser()
    parser.feed((PAGE_DIR / "index.html").read_text(encoding="utf-8"))
    assert parser.references
    for reference in parser.references:
        for candidate in reference.split(","):
            url = candidate.strip().split()[0]
            parsed = urlparse(url)
            assert parsed.scheme not in {"http", "https"}
            assert not parsed.netloc
            assert not url.startswith("//")

    assert len(parser.iframes) == 1
    preview_iframe = parser.iframes[0]
    assert "sandbox" in preview_iframe
    assert not preview_iframe["sandbox"]
    assert preview_iframe.get("referrerpolicy") == "no-referrer"

    frontend_sources = [
        ROOT / "frontend" / "index.html",
        ROOT / "frontend" / "vite.config.js",
        *(ROOT / "frontend" / "src").rglob("*"),
    ]
    external_url = re.compile(r"https?://|[\"']//", re.IGNORECASE)
    assert all(
        not external_url.search(path.read_text(encoding="utf-8"))
        for path in frontend_sources
        if path.is_file()
    )


def test_workers_are_inline_without_worker_chunks() -> None:
    scripts = list((PAGE_DIR / "assets").glob("*.js"))
    payload = b"\n".join(path.read_bytes() for path in scripts)
    inline_worker_blobs = re.findall(rb"\.Blob&&new Blob\(\[", payload)
    assert len(inline_worker_blobs) >= 4
    assert b"createObjectURL" in payload

    worker_chunks = [
        path
        for path in (PAGE_DIR / "assets").rglob("*")
        if path.is_file() and "worker" in path.name.lower()
    ]
    assert worker_chunks == []


def test_package_includes_runtime_page_but_not_frontend_sources(tmp_path: Path) -> None:
    package_plugin = load_package_module()
    package_files = package_plugin.iter_package_files()

    required = {
        "pages/template-editor/index.html",
        "pages/template-editor/vendor/monaco/LICENSE",
        "pages/template-editor/vendor/monaco/ThirdPartyNotices.txt",
        ".astrbot-plugin/i18n/zh-CN.json",
        ".astrbot-plugin/i18n/en-US.json",
    }
    assert required.issubset(package_files)
    assert any(
        path.startswith("pages/template-editor/assets/") for path in package_files
    )
    assert all(not path.startswith("frontend/") for path in package_files)
    assert all("node_modules" not in path for path in package_files)

    output = tmp_path / "phase1-template-page-test.zip"
    package_plugin.build_archive(
        output, flat=False, package_version="0.2.0-test.phase1-template-page"
    )
    assert output.stat().st_size < SIZE_LIMIT
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
    assert any(name.endswith("/pages/template-editor/index.html") for name in names)
    assert all(
        "/frontend/" not in name and "node_modules" not in name for name in names
    )


def test_built_page_size_is_below_plugin_page_limit() -> None:
    assert sum(path.stat().st_size for path in built_files()) < SIZE_LIMIT


def test_plugin_page_i18n_schema() -> None:
    for locale in ("zh-CN", "en-US"):
        data = json.loads(
            (ROOT / ".astrbot-plugin" / "i18n" / f"{locale}.json").read_text(
                encoding="utf-8"
            )
        )
        page = data["pages"]["template-editor"]
        assert isinstance(page["title"], str) and page["title"].strip()
        assert isinstance(page["description"], str) and page["description"].strip()
