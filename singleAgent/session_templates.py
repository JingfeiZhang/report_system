"""全局可写报告模板库（增删改查，与 session 无关）。

目录结构::

    logs/custom_report_templates/
      manifest.json
      user/<slug>.md          # 三合一：## 模板说明 / ## 效果预览 / ## 生成要求

合并规则：系统内置 ∪ 自定义；同 slug 时自定义覆盖；
``hidden_system_ids`` 可软隐藏系统模板。
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from loguru import logger

from singleAgent.config import CUSTOM_REPORT_TEMPLATES_DIR, agent_template_source
from singleAgent.report_template_loader import (
    ReportTemplate,
    TemplateConfigurationError,
    _MIN_OUTPUT_TOKENS,
    _MAX_OUTPUT_TOKENS,
    _parse_template,
    compose_unified_template_md,
    split_unified_template_md,
    load_builtin_report_templates as _load_system_templates,
    load_builtin_report_template as _load_system_template,
)

_MANIFEST_VERSION = 1
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_FILE_LOCK = threading.RLock()

# 兼容旧环境变量名
SESSION_TEMPLATES_DIR = CUSTOM_REPORT_TEMPLATES_DIR


class SessionTemplateError(ValueError):
    """业务可对前端展示的模板错误（兼容旧类名）。"""


TemplateError = SessionTemplateError


@dataclass
class TemplateManifest:
    version: int = _MANIFEST_VERSION
    hidden_system_ids: List[str] = field(default_factory=list)
    templates: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "hidden_system_ids": list(self.hidden_system_ids),
            "templates": list(self.templates),
        }


# 兼容旧名
SessionTemplateManifest = TemplateManifest


def custom_templates_root(*, base_dir: Optional[Path] = None) -> Path:
    return Path(base_dir) if base_dir is not None else Path(CUSTOM_REPORT_TEMPLATES_DIR)


def session_templates_root(
    session_id: str = "",
    *,
    base_dir: Optional[Path] = None,
) -> Path:
    """兼容旧调用：忽略 session_id，返回全局模板根目录。"""
    return custom_templates_root(base_dir=base_dir)


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text.rstrip() + "\n", encoding="utf-8")
    tmp.replace(path)


def _load_manifest(store_dir: Path) -> TemplateManifest:
    path = store_dir / "manifest.json"
    if not path.is_file():
        return TemplateManifest()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"自定义模板清单损坏 {path}: {exc}")
        return TemplateManifest()
    if not isinstance(raw, dict):
        return TemplateManifest()
    hidden = raw.get("hidden_system_ids") or []
    templates = raw.get("templates") or []
    if not isinstance(hidden, list):
        hidden = []
    if not isinstance(templates, list):
        templates = []
    return TemplateManifest(
        version=int(raw.get("version") or _MANIFEST_VERSION),
        hidden_system_ids=[str(x).strip() for x in hidden if str(x).strip()],
        templates=[t for t in templates if isinstance(t, dict)],
    )


def _save_manifest(store_dir: Path, manifest: TemplateManifest) -> None:
    _atomic_write_json(store_dir / "manifest.json", manifest.to_dict())


def _validate_slug(template_id: str) -> str:
    tid = (template_id or "").strip()
    if not _ID_RE.match(tid):
        raise SessionTemplateError(
            "模板 id 须匹配 ^[a-z][a-z0-9_]{1,63}$（小写字母开头）"
        )
    return tid


def _validate_contents(
    *,
    name: str,
    guidance: str = "",
    preview: str = "",
    generation: str = "",
    content: str = "",
    max_output_tokens: Any,
) -> Tuple[str, str, str, str, int, str]:
    """校验并返回 (name, guidance, preview, generation, tokens, disk_md)。

    优先 ``content``（完整 md，甲方自由格式）；
    否则兼容 guidance/preview/generation 三段。
    """
    name_s = (name or "").strip()
    if not name_s:
        raise SessionTemplateError("name 不能为空")
    try:
        tokens = int(max_output_tokens)
    except (TypeError, ValueError) as exc:
        raise SessionTemplateError("max_output_tokens 必须是整数") from exc
    if not _MIN_OUTPUT_TOKENS <= tokens <= _MAX_OUTPUT_TOKENS:
        raise SessionTemplateError(
            f"max_output_tokens 须在 {_MIN_OUTPUT_TOKENS}～{_MAX_OUTPUT_TOKENS} 之间"
        )

    raw = (content or "").strip()
    if raw:
        parts = split_unified_template_md(raw)
        if parts:
            return (
                name_s,
                parts["guidance"],
                parts["preview"],
                parts["generation"],
                tokens,
                raw + ("\n" if not raw.endswith("\n") else ""),
            )
        # 整份 md 即模板正文（右栏 + 模型）
        return (
            name_s,
            f"模板：{name_s}",
            raw,
            raw,
            tokens,
            raw + ("\n" if not raw.endswith("\n") else ""),
        )

    g = (guidance or "").strip()
    p = (preview or "").strip()
    gen = (generation or "").strip()
    if g and p and gen:
        disk = compose_unified_template_md(
            guidance=g, preview=p, generation=gen
        )
        return name_s, g, p, gen, tokens, disk
    if p and not g and not gen:
        # 只给预览正文，同样视为自由格式
        return (
            name_s,
            f"模板：{name_s}",
            p,
            p,
            tokens,
            p + ("\n" if not p.endswith("\n") else ""),
        )
    raise SessionTemplateError(
        "请提供 content（完整 Markdown），或同时提供 guidance / preview / generation"
    )


def _system_id_set(project_root: Optional[Path] = None) -> Set[str]:
    try:
        return {t.id for t in _load_system_templates(project_root=project_root)}
    except Exception as exc:
        logger.warning(f"加载系统模板失败: {exc}")
        return set()


def _entry_content_file(template_id: str) -> str:
    return f"user/{template_id}.md"


def _write_template_files(
    store_dir: Path,
    template_id: str,
    *,
    disk_md: str,
) -> Dict[str, str]:
    rel = _entry_content_file(template_id)
    _atomic_write_text(store_dir / rel, disk_md.rstrip() + "\n")
    _remove_legacy_template_dir(store_dir, template_id)
    return {"content_file": rel}


def _remove_legacy_template_dir(store_dir: Path, template_id: str) -> None:
    folder = store_dir / "user" / template_id
    if not folder.is_dir():
        return
    for child in folder.iterdir():
        if child.is_file():
            child.unlink(missing_ok=True)  # type: ignore[call-arg]
    try:
        folder.rmdir()
    except OSError:
        pass


def _remove_template_files(store_dir: Path, template_id: str) -> None:
    md = store_dir / "user" / f"{template_id}.md"
    if md.is_file():
        md.unlink(missing_ok=True)  # type: ignore[call-arg]
    _remove_legacy_template_dir(store_dir, template_id)


def _find_entry(
    manifest: TemplateManifest, template_id: str
) -> Optional[Dict[str, Any]]:
    for entry in manifest.templates:
        if str(entry.get("id") or "") == template_id:
            return entry
    return None


def template_to_api_dict(
    template: ReportTemplate,
    *,
    origin: str,
    include_content: bool = True,
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "id": template.id,
        "name": template.name,
        "tagline": template.tagline,
        "length_label": template.length_label,
        "max_output_tokens": template.max_output_tokens,
        "recommended": template.recommended,
        "sort_order": template.sort_order,
        "tags": list(template.tags),
        "origin": origin,
        "editable": True,
        "deletable": True,
        "restorable": origin in ("system", "system_override"),
    }
    if include_content:
        data["guidance"] = template.guidance_content
        data["preview"] = template.preview_content
        data["generation"] = template.generation_content
        # 对外主字段：一份完整 md（与落盘一致时优先自由格式）
        if (
            template.preview_content == template.generation_content
            and template.guidance_content.startswith("模板：")
        ):
            data["content"] = template.preview_content
        else:
            data["content"] = compose_unified_template_md(
                guidance=template.guidance_content,
                preview=template.preview_content,
                generation=template.generation_content,
            ).rstrip()
    return data


def list_templates(
    *,
    project_root: Optional[Path] = None,
    base_dir: Optional[Path] = None,
    include_content: bool = True,
) -> List[Dict[str, Any]]:
    """合并系统 + 全局自定义模板。"""
    with _FILE_LOCK:
        store_dir = custom_templates_root(base_dir=base_dir)
        manifest = _load_manifest(store_dir)
        hidden = set(manifest.hidden_system_ids)
        system_ids = _system_id_set(project_root)
        custom_by_id = {
            str(e.get("id")): e for e in manifest.templates if e.get("id")
        }

        merged: List[Tuple[int, str, Dict[str, Any]]] = []
        for sys_t in _load_system_templates(project_root=project_root):
            if sys_t.id in hidden and sys_t.id not in custom_by_id:
                continue
            if sys_t.id in custom_by_id:
                entry = custom_by_id.pop(sys_t.id)
                try:
                    t = _parse_template(store_dir, entry)
                except Exception as exc:
                    logger.warning(f"跳过覆盖模板 {sys_t.id}: {exc}")
                    t = sys_t
                    origin = "system"
                else:
                    origin = "system_override"
                merged.append(
                    (
                        t.sort_order,
                        t.id,
                        template_to_api_dict(
                            t, origin=origin, include_content=include_content
                        ),
                    )
                )
            else:
                merged.append(
                    (
                        sys_t.sort_order,
                        sys_t.id,
                        template_to_api_dict(
                            sys_t, origin="system", include_content=include_content
                        ),
                    )
                )

        for tid, entry in custom_by_id.items():
            try:
                t = _parse_template(store_dir, entry)
            except Exception as exc:
                logger.warning(f"跳过自定义模板 {tid}: {exc}")
                continue
            merged.append(
                (
                    t.sort_order,
                    t.id,
                    template_to_api_dict(
                        t, origin="custom", include_content=include_content
                    ),
                )
            )

        merged.sort(key=lambda x: (x[0], x[1]))
        return [item for _, _, item in merged]


def get_template(
    template_id: str,
    *,
    project_root: Optional[Path] = None,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    tid = _validate_slug(template_id)
    for item in list_templates(
        project_root=project_root,
        base_dir=base_dir,
        include_content=True,
    ):
        if item["id"] == tid:
            return item
    raise SessionTemplateError(f"模板不存在或已隐藏: {tid}")


def create_template(
    payload: Dict[str, Any],
    *,
    project_root: Optional[Path] = None,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    raw_id = str(payload.get("id") or "").strip()
    if raw_id:
        tid = _validate_slug(raw_id)
    else:
        tid = f"custom_{uuid.uuid4().hex[:8]}"

    name, guidance, preview, generation, tokens, disk_md = _validate_contents(
        name=str(payload.get("name") or ""),
        guidance=str(payload.get("guidance") or ""),
        preview=str(payload.get("preview") or ""),
        generation=str(payload.get("generation") or ""),
        content=str(payload.get("content") or ""),
        max_output_tokens=payload.get("max_output_tokens", 1400),
    )

    with _FILE_LOCK:
        store_dir = custom_templates_root(base_dir=base_dir)
        manifest = _load_manifest(store_dir)
        system_ids = _system_id_set(project_root)
        if tid in system_ids:
            raise SessionTemplateError(
                f"id={tid} 与系统模板冲突；请改用 PUT 覆盖，或换 custom_ 前缀 id"
            )
        if _find_entry(manifest, tid):
            raise SessionTemplateError(f"自定义模板已存在: {tid}")

        paths = _write_template_files(store_dir, tid, disk_md=disk_md)
        entry = {
            "id": tid,
            "name": name,
            "tagline": str(payload.get("tagline") or "").strip(),
            "length_label": str(payload.get("length_label") or "").strip(),
            "max_output_tokens": tokens,
            "recommended": bool(payload.get("recommended", False)),
            "sort_order": int(payload.get("sort_order") or 100),
            "tags": [str(t) for t in (payload.get("tags") or [])],
            "content_file": paths["content_file"],
        }
        manifest.templates.append(entry)
        _save_manifest(store_dir, manifest)
        return get_template(tid, project_root=project_root, base_dir=base_dir)


def update_template(
    template_id: str,
    payload: Dict[str, Any],
    *,
    project_root: Optional[Path] = None,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    tid = _validate_slug(template_id)
    existing = get_template(tid, project_root=project_root, base_dir=base_dir)
    # content 优先；否则沿用/合并三段字段
    if "content" in payload and str(payload.get("content") or "").strip():
        name, guidance, preview, generation, tokens, disk_md = _validate_contents(
            name=str(payload.get("name") if "name" in payload else existing["name"]),
            content=str(payload.get("content") or ""),
            max_output_tokens=(
                payload.get("max_output_tokens")
                if "max_output_tokens" in payload
                else existing["max_output_tokens"]
            ),
        )
    else:
        name, guidance, preview, generation, tokens, disk_md = _validate_contents(
            name=str(payload.get("name") if "name" in payload else existing["name"]),
            guidance=str(
                payload.get("guidance")
                if "guidance" in payload
                else existing.get("guidance") or ""
            ),
            preview=str(
                payload.get("preview")
                if "preview" in payload
                else existing.get("preview") or ""
            ),
            generation=str(
                payload.get("generation")
                if "generation" in payload
                else existing.get("generation") or ""
            ),
            max_output_tokens=(
                payload.get("max_output_tokens")
                if "max_output_tokens" in payload
                else existing["max_output_tokens"]
            ),
        )

    with _FILE_LOCK:
        store_dir = custom_templates_root(base_dir=base_dir)
        manifest = _load_manifest(store_dir)
        system_ids = _system_id_set(project_root)
        entry = _find_entry(manifest, tid)
        paths = _write_template_files(store_dir, tid, disk_md=disk_md)
        new_entry = {
            "id": tid,
            "name": name,
            "tagline": str(
                payload.get("tagline")
                if "tagline" in payload
                else existing.get("tagline") or ""
            ).strip(),
            "length_label": str(
                payload.get("length_label")
                if "length_label" in payload
                else existing.get("length_label") or ""
            ).strip(),
            "max_output_tokens": tokens,
            "recommended": bool(
                payload.get("recommended")
                if "recommended" in payload
                else existing.get("recommended", False)
            ),
            "sort_order": int(
                payload.get("sort_order")
                if "sort_order" in payload
                else existing.get("sort_order") or 100
            ),
            "tags": [
                str(t)
                for t in (
                    payload.get("tags")
                    if "tags" in payload
                    else existing.get("tags") or []
                )
            ],
            "content_file": paths["content_file"],
        }
        if entry is None:
            if tid not in system_ids:
                raise SessionTemplateError(f"模板不存在: {tid}")
            manifest.templates.append(new_entry)
            if tid in manifest.hidden_system_ids:
                manifest.hidden_system_ids = [
                    x for x in manifest.hidden_system_ids if x != tid
                ]
        else:
            for i, item in enumerate(manifest.templates):
                if str(item.get("id")) == tid:
                    manifest.templates[i] = new_entry
                    break
        _save_manifest(store_dir, manifest)
        return get_template(tid, project_root=project_root, base_dir=base_dir)


def delete_template(
    template_id: str,
    *,
    project_root: Optional[Path] = None,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    tid = _validate_slug(template_id)
    with _FILE_LOCK:
        store_dir = custom_templates_root(base_dir=base_dir)
        manifest = _load_manifest(store_dir)
        system_ids = _system_id_set(project_root)
        entry = _find_entry(manifest, tid)

        if entry is not None:
            manifest.templates = [
                e for e in manifest.templates if str(e.get("id")) != tid
            ]
            _remove_template_files(store_dir, tid)
            _save_manifest(store_dir, manifest)
            return {
                "id": tid,
                "action": "removed_custom_copy",
                "system_restored": tid in system_ids,
            }

        if tid in system_ids:
            if tid not in manifest.hidden_system_ids:
                manifest.hidden_system_ids.append(tid)
            _save_manifest(store_dir, manifest)
            return {"id": tid, "action": "hidden_system", "system_restored": False}

        raise SessionTemplateError(f"模板不存在: {tid}")


def restore_template(
    template_id: str,
    *,
    project_root: Optional[Path] = None,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """恢复被软隐藏的系统模板；若存在覆盖副本则先删覆盖。"""
    tid = _validate_slug(template_id)
    system_ids = _system_id_set(project_root)
    if tid not in system_ids:
        raise SessionTemplateError(f"仅系统模板可 restore: {tid}")

    with _FILE_LOCK:
        store_dir = custom_templates_root(base_dir=base_dir)
        manifest = _load_manifest(store_dir)
        if _find_entry(manifest, tid):
            manifest.templates = [
                e for e in manifest.templates if str(e.get("id")) != tid
            ]
            _remove_template_files(store_dir, tid)
        manifest.hidden_system_ids = [
            x for x in manifest.hidden_system_ids if x != tid
        ]
        _save_manifest(store_dir, manifest)
        return get_template(tid, project_root=project_root, base_dir=base_dir)


def _load_local_merged_report_templates(
    *,
    project_root: Optional[Path] = None,
    base_dir: Optional[Path] = None,
) -> List[ReportTemplate]:
    items = list_templates(
        project_root=project_root,
        base_dir=base_dir,
        include_content=True,
    )
    out: List[ReportTemplate] = []
    for item in items:
        out.append(
            ReportTemplate(
                id=item["id"],
                name=item["name"],
                tagline=item.get("tagline") or "",
                length_label=item.get("length_label") or "",
                tags=list(item.get("tags") or []),
                recommended=bool(item.get("recommended")),
                sort_order=int(item.get("sort_order") or 0),
                max_output_tokens=int(item["max_output_tokens"]),
                guidance_content=item["guidance"],
                preview_content=item["preview"],
                generation_content=item["generation"],
            )
        )
    return out


def load_merged_report_templates(
    *,
    session_id: Optional[str] = None,  # 忽略；保留签名兼容
    project_root: Optional[Path] = None,
    base_dir: Optional[Path] = None,
) -> List[ReportTemplate]:
    """供 event2 推模板 / 选模板：优先卫鼎 remote；空/失败则回退本地。

    远程模板库不归本服务管理：不可因对方为空或宕机导致本服务不可用。
    """
    del session_id  # unused
    if agent_template_source() == "remote":
        try:
            from singleAgent.agent_template_client import (
                fetch_open_template_list,
                remote_templates_configured,
            )

            if remote_templates_configured():
                remote = fetch_open_template_list()
                if remote:
                    return list(remote)
                logger.warning("卫鼎模板 list 为空，回退本地模板库")
            else:
                logger.warning(
                    "AGENT_TEMPLATE_SOURCE=remote 但未配置 BASE_URL，回退本地模板库"
                )
        except Exception as exc:
            logger.error("卫鼎模板 list 失败，回退本地模板库: {}", exc)
    return _load_local_merged_report_templates(
        project_root=project_root, base_dir=base_dir
    )


def load_merged_report_template(
    template_id: Optional[str] = None,
    *,
    session_id: Optional[str] = None,  # 忽略
    project_root: Optional[Path] = None,
    base_dir: Optional[Path] = None,
) -> ReportTemplate:
    del session_id
    wanted = str(template_id).strip() if template_id is not None else ""

    def _from_local() -> ReportTemplate:
        templates = _load_local_merged_report_templates(
            project_root=project_root,
            base_dir=base_dir,
        )
        if not wanted:
            if not templates:
                return _load_system_template(None, project_root=project_root)
            for t in templates:
                if t.recommended:
                    return t
            return templates[0]
        for t in templates:
            if t.id == wanted:
                return t
        raise TemplateConfigurationError(f"未知报告模板: {wanted}")

    if agent_template_source() == "remote":
        from singleAgent.agent_template_client import (
            fetch_open_template_detail,
            fetch_open_template_list,
            remote_templates_configured,
        )

        if not remote_templates_configured():
            logger.warning(
                "AGENT_TEMPLATE_SOURCE=remote 但未配置 BASE_URL，回退本地模板"
            )
            return _from_local()
        try:
            if wanted:
                try:
                    return fetch_open_template_detail(wanted)
                except Exception as exc:
                    logger.warning(
                        "卫鼎模板 detail id={} 失败，尝试 list 匹配: {}",
                        wanted,
                        exc,
                    )
                    try:
                        for t in fetch_open_template_list():
                            if t.id == wanted:
                                return t
                    except Exception as list_exc:
                        logger.warning(
                            "卫鼎模板 list 匹配也失败: {}", list_exc
                        )
                    logger.warning(
                        "卫鼎无模板 id={}，回退本地同名/默认模板", wanted
                    )
                    try:
                        return _from_local()
                    except TemplateConfigurationError as local_exc:
                        raise TemplateConfigurationError(
                            f"卫鼎模板不存在或不可用: {wanted}"
                        ) from local_exc
            remote = fetch_open_template_list()
            if remote:
                for t in remote:
                    if t.recommended:
                        return t
                return remote[0]
            logger.warning("卫鼎模板 list 为空，回退本地模板")
            return _from_local()
        except TemplateConfigurationError as exc:
            # 卫鼎业务码/坏 JSON 也是「别人的错」：无指定 id 时回退本地
            if wanted:
                logger.warning(
                    "卫鼎模板业务失败 id={}，尝试本地: {}", wanted, exc
                )
                try:
                    return _from_local()
                except TemplateConfigurationError as local_exc:
                    raise TemplateConfigurationError(
                        f"卫鼎模板不存在或不可用: {wanted}"
                    ) from local_exc
            logger.error("卫鼎模板业务失败，回退本地: {}", exc)
            return _from_local()
        except Exception as exc:
            logger.error("卫鼎模板加载失败，回退本地: {}", exc)
            return _from_local()

    return _from_local()


# —— 旧函数名兼容（忽略 session_id）——
def list_session_templates(session_id: str = "", **kwargs: Any) -> List[Dict[str, Any]]:
    return list_templates(**kwargs)


def get_session_template(session_id: str, template_id: str, **kwargs: Any) -> Dict[str, Any]:
    return get_template(template_id, **kwargs)


def create_session_template(session_id: str, payload: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    return create_template(payload, **kwargs)


def update_session_template(
    session_id: str, template_id: str, payload: Dict[str, Any], **kwargs: Any
) -> Dict[str, Any]:
    return update_template(template_id, payload, **kwargs)


def delete_session_template(session_id: str, template_id: str, **kwargs: Any) -> Dict[str, Any]:
    return delete_template(template_id, **kwargs)


def restore_session_template(session_id: str, template_id: str, **kwargs: Any) -> Dict[str, Any]:
    return restore_template(template_id, **kwargs)
