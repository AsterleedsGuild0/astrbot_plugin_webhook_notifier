from __future__ import annotations

import json
import os
import shutil
import threading
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .renderer import DEFAULT_HTML_TEMPLATE, validate_html_template

REGISTRY_VERSION = 1
BUILT_IN_ID = "built-in"
BUILT_IN_WIDTH = 812
MAX_TEMPLATES = 100
MAX_TEMPLATE_BYTES = 512 * 1024


class TemplateRegistryError(ValueError):
    """模板 Registry 操作失败。"""


class TemplateConflictError(TemplateRegistryError):
    """模板 revision 与 expected_revision 不一致。"""


class TemplateReadOnlyError(TemplateRegistryError):
    """Registry 版本未知，当前只能读取。"""


@dataclass(frozen=True)
class ActiveTemplate:
    """一次渲染所需的不可变模板快照。"""

    id: str
    display_name: str
    content: str
    canvas_width: int
    revision: int
    updated_at: str
    valid: bool = True


@dataclass(frozen=True)
class RegistrySnapshot:
    """TemplateRegistry 的不可变内存快照。"""

    version: int
    active: str
    effective_active: str
    templates: Mapping[str, Mapping[str, Any]]
    read_only: bool = False


class TemplateRegistry:
    """管理用户 HTML 模板及原子持久化。"""

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self._lock = threading.RLock()
        self._root = Path(data_dir)
        self._templates_dir = self._root / "templates"
        self._registry_path = self._root / "templates.json"
        self._root.mkdir(parents=True, exist_ok=True)
        self._ensure_safe_directory(self._root)
        self._templates_dir.mkdir(exist_ok=True)
        self._ensure_safe_directory(self._templates_dir)
        self._contents: Mapping[str, str] = MappingProxyType({})
        self._snapshot = RegistrySnapshot(
            REGISTRY_VERSION,
            BUILT_IN_ID,
            BUILT_IN_ID,
            MappingProxyType({}),
        )
        self._load()

    @property
    def snapshot(self) -> RegistrySnapshot:
        return self._snapshot

    @property
    def read_only(self) -> bool:
        return self._snapshot.read_only

    def list_templates(self) -> list[dict[str, Any]]:
        """返回 built-in 与自定义模板摘要。"""
        snapshot = self._snapshot
        active = snapshot.active
        built_in = {
            "id": BUILT_IN_ID,
            "display_name": "Built-in",
            "canvas_width": BUILT_IN_WIDTH,
            "revision": 0,
            "updated_at": "",
            "built_in": True,
            "active": active == BUILT_IN_ID,
            "valid": True,
            "read_only": True,
        }
        items = [built_in]
        for template_id, record in snapshot.templates.items():
            item = {"id": template_id, **dict(record)}
            item.pop("file", None)
            item["built_in"] = False
            item["active"] = template_id == active
            item["read_only"] = False
            items.append(item)
        return items

    def get(self, template_id: str) -> ActiveTemplate | None:
        """按 ID 返回 content 与 width 属于同一快照的模板。"""
        with self._lock:
            snapshot = self._snapshot
            contents = self._contents
            if template_id == BUILT_IN_ID:
                return self._built_in()
            record = snapshot.templates.get(template_id)
            content = contents.get(template_id)
            if not record or content is None:
                return None
            return ActiveTemplate(
                id=template_id,
                display_name=str(record["display_name"]),
                content=content,
                canvas_width=int(record["canvas_width"]),
                revision=int(record["revision"]),
                updated_at=str(record["updated_at"]),
                valid=bool(record.get("valid", False)),
            )

    def get_active(self) -> ActiveTemplate:
        """返回 effective active 模板快照。"""
        with self._lock:
            return self.get(self._snapshot.effective_active) or self._built_in()

    def save(
        self,
        template_id: str | None,
        display_name: Any,
        content: Any,
        canvas_width: Any,
        expected_revision: Any = None,
        apply: bool = False,
    ) -> ActiveTemplate:
        """创建或保存模板，并可在同一 Registry 提交中应用。"""
        display_name = self._validate_display_name(display_name)
        canvas_width = self._validate_canvas_width(canvas_width)
        content = self._validate_content(content)
        with self._lock:
            self._require_writable()
            current = self._snapshot
            creating = template_id in (None, "")
            if creating:
                if len(current.templates) >= MAX_TEMPLATES:
                    raise TemplateRegistryError("模板数量已达到上限")
                if expected_revision is not None:
                    raise TemplateConflictError("新模板不能指定 expected_revision")
                template_id = f"custom-{uuid.uuid4()}"
                revision = 1
            else:
                if template_id == BUILT_IN_ID or template_id not in current.templates:
                    raise TemplateRegistryError("模板不存在或不可写")
                old = current.templates[template_id]
                if (
                    not isinstance(expected_revision, int)
                    or isinstance(expected_revision, bool)
                    or expected_revision != old["revision"]
                ):
                    raise TemplateConflictError("模板已被其他请求修改")
                revision = int(old["revision"]) + 1

            filename = f"{template_id}-{revision}.html"
            revision_path = self._safe_revision_path(filename)
            self._atomic_write(revision_path, content.encode("utf-8"))
            now = datetime.now(timezone.utc).isoformat()
            records = {key: dict(value) for key, value in current.templates.items()}
            records[template_id] = {
                "display_name": display_name,
                "file": filename,
                "canvas_width": canvas_width,
                "revision": revision,
                "updated_at": now,
                "valid": True,
            }
            active = template_id if apply else current.active
            next_data = {
                "version": REGISTRY_VERSION,
                "active": active,
                "templates": {
                    key: {k: v for k, v in value.items() if k != "valid"}
                    for key, value in records.items()
                },
            }
            try:
                self._commit_registry(next_data)
            except Exception:
                self._best_effort_unlink(revision_path)
                raise
            contents = dict(self._contents)
            contents[template_id] = content
            self._replace_snapshot(active, records, contents)
            self._clean_unused_files()
            return self.get(template_id)  # type: ignore[return-value]

    def apply(self, template_id: Any, expected_revision: Any = None) -> ActiveTemplate:
        """应用一个已保存且有效的模板。"""
        with self._lock:
            self._require_writable()
            if template_id == BUILT_IN_ID:
                if expected_revision not in (None, 0):
                    raise TemplateConflictError("built-in revision 冲突")
            else:
                record = self._snapshot.templates.get(str(template_id))
                if not record or not record.get("valid"):
                    raise TemplateRegistryError("模板不存在或无效")
                if (
                    not isinstance(expected_revision, int)
                    or isinstance(expected_revision, bool)
                    or expected_revision != record["revision"]
                ):
                    raise TemplateConflictError("模板已被其他请求修改")
            next_data = self._serializable_registry(str(template_id))
            self._commit_registry(next_data)
            self._replace_snapshot(
                str(template_id),
                {k: dict(v) for k, v in self._snapshot.templates.items()},
                dict(self._contents),
            )
            return self.get_active()

    def delete(self, template_id: Any, expected_revision: Any) -> None:
        """先提交 Registry，再 best-effort 删除 revision 文件。"""
        with self._lock:
            self._require_writable()
            template_id = str(template_id)
            if (
                template_id == BUILT_IN_ID
                or template_id not in self._snapshot.templates
            ):
                raise TemplateRegistryError("模板不存在或不可删除")
            if self._snapshot.active == template_id:
                raise TemplateRegistryError("不能删除 active 模板")
            record = self._snapshot.templates[template_id]
            if (
                not isinstance(expected_revision, int)
                or isinstance(expected_revision, bool)
                or expected_revision != record["revision"]
            ):
                raise TemplateConflictError("模板已被其他请求修改")
            records = {k: dict(v) for k, v in self._snapshot.templates.items()}
            removed = records.pop(template_id)
            next_data = {
                "version": REGISTRY_VERSION,
                "active": self._snapshot.active,
                "templates": {
                    key: {k: v for k, v in value.items() if k != "valid"}
                    for key, value in records.items()
                },
            }
            self._commit_registry(next_data)
            contents = dict(self._contents)
            contents.pop(template_id, None)
            self._replace_snapshot(self._snapshot.active, records, contents)
            self._best_effort_unlink(self._safe_revision_path(str(removed["file"])))

    def _load(self) -> None:
        if not self._registry_path.exists():
            self._commit_registry(
                {"version": REGISTRY_VERSION, "active": BUILT_IN_ID, "templates": {}}
            )
            self._clean_unused_files()
            return
        if self._registry_path.is_symlink():
            raise TemplateRegistryError("templates.json 不允许为 symlink")
        try:
            data = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            try:
                shutil.copy2(
                    self._registry_path, f"{self._registry_path}.corrupt-{stamp}"
                )
            except OSError:
                pass
            self._commit_registry(
                {"version": REGISTRY_VERSION, "active": BUILT_IN_ID, "templates": {}}
            )
            self._clean_unused_files()
            return
        if not isinstance(data, dict) or data.get("version") != REGISTRY_VERSION:
            self._snapshot = RegistrySnapshot(
                int(data.get("version", -1)) if isinstance(data, dict) else -1,
                str(data.get("active", BUILT_IN_ID))
                if isinstance(data, dict)
                else BUILT_IN_ID,
                BUILT_IN_ID,
                MappingProxyType({}),
                True,
            )
            return
        raw_templates = data.get("templates")
        active = data.get("active")
        if not isinstance(raw_templates, dict) or not isinstance(active, str):
            self._recover_invalid_registry()
            return
        records: dict[str, dict[str, Any]] = {}
        contents: dict[str, str] = {}
        structurally_valid = True
        for template_id, raw in raw_templates.items():
            try:
                record, content = self._load_record(template_id, raw)
            except Exception:
                structurally_valid = False
                continue
            records[template_id] = record
            if content is not None:
                contents[template_id] = content
        if active != BUILT_IN_ID and active not in records:
            structurally_valid = False
        self._replace_snapshot(active, records, contents)
        if structurally_valid and all(r.get("valid") for r in records.values()):
            self._clean_unused_files()

    def _load_record(
        self, template_id: Any, raw: Any
    ) -> tuple[dict[str, Any], str | None]:
        if not isinstance(template_id, str) or not template_id.startswith("custom-"):
            raise TemplateRegistryError("非法模板 ID")
        if not isinstance(raw, dict):
            raise TemplateRegistryError("非法模板记录")
        record = {
            "display_name": self._validate_display_name(raw.get("display_name")),
            "file": str(raw.get("file", "")),
            "canvas_width": self._validate_canvas_width(raw.get("canvas_width")),
            "revision": raw.get("revision"),
            "updated_at": str(raw.get("updated_at", "")),
            "valid": False,
        }
        if (
            not isinstance(record["revision"], int)
            or isinstance(record["revision"], bool)
            or record["revision"] < 1
        ):
            raise TemplateRegistryError("非法 revision")
        expected_file = f"{template_id}-{record['revision']}.html"
        if record["file"] != expected_file:
            raise TemplateRegistryError("非法 revision 文件名")
        try:
            path = self._safe_revision_path(record["file"])
            if path.is_symlink() or not path.is_file():
                return record, None
            content = path.read_text(encoding="utf-8")
            self._validate_content(content)
        except Exception:
            return record, None
        record["valid"] = True
        return record, content

    def _replace_snapshot(
        self,
        active: str,
        records: dict[str, dict[str, Any]],
        contents: dict[str, str],
    ) -> None:
        immutable_records = MappingProxyType(
            {key: MappingProxyType(dict(value)) for key, value in records.items()}
        )
        effective = active
        if active != BUILT_IN_ID and (
            active not in records or not records[active].get("valid")
        ):
            effective = BUILT_IN_ID
        self._contents = MappingProxyType(dict(contents))
        self._snapshot = RegistrySnapshot(
            REGISTRY_VERSION, active, effective, immutable_records
        )

    def _serializable_registry(self, active: str) -> dict[str, Any]:
        return {
            "version": REGISTRY_VERSION,
            "active": active,
            "templates": {
                key: {k: v for k, v in dict(value).items() if k != "valid"}
                for key, value in self._snapshot.templates.items()
            },
        }

    def _recover_invalid_registry(self) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        try:
            shutil.copy2(self._registry_path, f"{self._registry_path}.corrupt-{stamp}")
        except OSError:
            pass
        self._commit_registry(
            {"version": REGISTRY_VERSION, "active": BUILT_IN_ID, "templates": {}}
        )
        self._replace_snapshot(BUILT_IN_ID, {}, {})

    def _commit_registry(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        self._atomic_write(self._registry_path, payload)

    def _atomic_write(self, target: Path, payload: bytes) -> None:
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temp, "xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
            try:
                directory_fd = os.open(str(target.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            self._best_effort_unlink(temp)

    def _clean_unused_files(self) -> None:
        referenced = {
            str(record["file"]) for record in self._snapshot.templates.values()
        }
        for path in self._templates_dir.iterdir():
            if path.is_symlink():
                continue
            if path.name not in referenced and (
                path.name.endswith(".html") or path.name.endswith(".tmp")
            ):
                self._best_effort_unlink(path)

    def _safe_revision_path(self, filename: str) -> Path:
        if not filename or filename != Path(filename).name:
            raise TemplateRegistryError("非法模板文件名")
        path = self._templates_dir / filename
        if path.parent.resolve() != self._templates_dir.resolve():
            raise TemplateRegistryError("模板路径逃逸")
        return path

    @staticmethod
    def _ensure_safe_directory(path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            raise TemplateRegistryError("模板目录必须是普通目录")

    @staticmethod
    def _validate_display_name(value: Any) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 80:
            raise TemplateRegistryError("display_name 长度必须为 1..80")
        if any(unicodedata.category(char) == "Cc" for char in value):
            raise TemplateRegistryError("display_name 不允许控制字符")
        return value

    @staticmethod
    def _validate_canvas_width(value: Any) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 320 <= value <= 2048
        ):
            raise TemplateRegistryError("canvas_width 必须是 320..2048 的整数")
        return value

    @staticmethod
    def _validate_content(value: Any) -> str:
        if (
            not isinstance(value, str)
            or len(value.encode("utf-8")) > MAX_TEMPLATE_BYTES
        ):
            raise TemplateRegistryError("模板必须是至多 512KiB 的字符串")
        validate_html_template(value)
        return value

    def _require_writable(self) -> None:
        if self._snapshot.read_only:
            raise TemplateReadOnlyError("未知 Registry version，当前为只读模式")

    @staticmethod
    def _best_effort_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _built_in() -> ActiveTemplate:
        return ActiveTemplate(
            BUILT_IN_ID,
            "Built-in",
            DEFAULT_HTML_TEMPLATE,
            BUILT_IN_WIDTH,
            0,
            "",
        )

    def export_template(self, template_id: str) -> dict[str, Any] | None:
        """返回适合 API 输出的完整模板。"""
        with self._lock:
            template = self.get(template_id)
            if template is None:
                return None
            result = asdict(template)
            result["built_in"] = template_id == BUILT_IN_ID
            result["active"] = template_id == self._snapshot.active
            return result
