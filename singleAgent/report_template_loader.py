"""Load report templates (one md per template, optional three-part sections)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

_DEFAULT_TEMPLATE_ID = os.getenv(
    "SINGLE_AGENT_REPORT_TEMPLATE",
    os.getenv("SINGLE_AGENT_DEFAULT_REPORT_TEMPLATE", "flexible_assistant"),
)
_MANIFEST_VERSION = 5
_CONTENT_KEYS = ("guidance", "preview", "generation")
_MIN_OUTPUT_TOKENS = 600
_MAX_OUTPUT_TOKENS = 1600

# 单文件内分节：只认这三个标题，正文里其它 ## 不切开
_SECTION_TITLE_TO_KEY = {
    "模板说明": "guidance",
    "效果预览": "preview",
    "生成要求": "generation",
    "guidance": "guidance",
    "preview": "preview",
    "generation": "generation",
}
_SECTION_MARKER_RE = re.compile(
    r"(?m)^##\s+(模板说明|效果预览|生成要求|Guidance|Preview|Generation)\s*$"
)


class TemplateConfigurationError(ValueError):
    """Raised when a report template manifest or content file is invalid."""


@dataclass(frozen=True)
class ReportTemplate:
    id: str
    name: str
    tagline: str
    length_label: str
    tags: List[str]
    recommended: bool
    sort_order: int
    max_output_tokens: int
    guidance_content: str
    preview_content: str
    generation_content: str

    def to_prompt_block(self, *, topic: str) -> str:
        current_topic = topic.strip() or "（未指定）"
        return "\n".join([
            f"【输出模板：{self.name}（{self.id}）】",
            self.tagline,
            f"建议篇幅：{self.length_label}",
            f"输出上限：{self.max_output_tokens} tokens，达到要求后立即结束。",
            "",
            "【当前分析主题】",
            current_topic,
            "",
            "【模板内容使用边界】",
            "1. 模板说明和生成要求不得原样复述给用户。",
            "2. 效果预览仅用于学习版式和表达风格。",
            "3. 效果预览是真实事件的版式示例，不代表当前事实状态；不得把其中事件名称、报告标题、日期、事实、数字或结论迁移到本轮分析主题。",
            "4. 正式报告标题必须围绕【当前分析主题】生成，不得沿用效果预览中的报告标题。",
            "",
            "【第一部分：模板说明】",
            self.guidance_content,
            "",
            "【第二部分：效果预览】",
            self.preview_content,
            "",
            "【第三部分：生成要求】",
            self.generation_content,
        ])

    def to_knowledge_ref(self) -> Dict[str, Any]:
        """Build the frontend template ref without exposing hidden prompt content."""
        return {
            "id": self.id,
            "content": self.preview_content,
            "score": 1.0,
            "source": self.name,
            "category": "报告版式",
            "dataset_id": self.id,
        }


def compose_unified_template_md(
    *,
    guidance: str,
    preview: str,
    generation: str,
) -> str:
    """三合一落盘格式。"""
    return (
        f"## 模板说明\n\n{(guidance or '').strip()}\n\n"
        f"## 效果预览\n\n{(preview or '').strip()}\n\n"
        f"## 生成要求\n\n{(generation or '').strip()}\n"
    )


def split_unified_template_md(text: str) -> Optional[Dict[str, str]]:
    """按固定分节标题拆成三份；正文内其它 ## 不影响。"""
    raw = (text or "").strip()
    if not raw:
        return None
    matches = list(_SECTION_MARKER_RE.finditer(raw))
    if len(matches) < 3:
        return None

    sections: Dict[str, str] = {}
    for i, match in enumerate(matches):
        key = _SECTION_TITLE_TO_KEY.get(match.group(1).strip())
        if not key:
            continue
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[start:end].strip()
        if body:
            sections[key] = body

    if all(sections.get(k) for k in _CONTENT_KEYS):
        return {k: sections[k] for k in _CONTENT_KEYS}
    return None


def _templates_root(project_root: Optional[Path] = None) -> Path:
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parent.parent
    return root / "report_templates"


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise TemplateConfigurationError(f"模板 JSON 必须是对象: {path}")
    return data


def _load_manifest(project_root: Optional[Path] = None) -> Dict[str, Any]:
    root = _templates_root(project_root)
    path = root / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"未找到模板清单: {path}")
    manifest = _read_json(path)
    ver = manifest.get("version")
    if ver not in (_MANIFEST_VERSION, 4):
        raise TemplateConfigurationError(
            f"模板清单版本必须为 {_MANIFEST_VERSION}（兼容 4）: {path}"
        )
    if not isinstance(manifest.get("templates"), list):
        raise TemplateConfigurationError(f"模板清单 templates 必须是数组: {path}")
    return manifest


def _read_path_under_root(
    templates_root: Path,
    *,
    template_id: str,
    label: str,
    relative_path: Any,
) -> str:
    raw_path = str(relative_path or "").strip()
    if not raw_path:
        raise TemplateConfigurationError(
            f"模板 {template_id} 缺少 {label}"
        )

    rel_path = Path(raw_path)
    if rel_path.is_absolute():
        raise TemplateConfigurationError(
            f"模板 {template_id} 的 {label} 路径必须位于模板根内: {raw_path}"
        )

    root = templates_root.resolve()
    path = (templates_root / rel_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise TemplateConfigurationError(
            f"模板 {template_id} 的 {label} 路径越界: {raw_path}"
        ) from exc

    if not path.is_file():
        raise FileNotFoundError(
            f"模板 {template_id} 的 {label} 文件不存在: {path}"
        )
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise TemplateConfigurationError(
            f"模板 {template_id} 的 {label} 文件为空: {path}"
        )
    return content


def _read_content_file(
    templates_root: Path,
    *,
    template_id: str,
    content_type: str,
    relative_path: Any,
) -> str:
    return _read_path_under_root(
        templates_root,
        template_id=template_id,
        label=f"content_files.{content_type}",
        relative_path=relative_path,
    )


def _load_contents(
    templates_root: Path,
    data: Dict[str, Any],
    *,
    template_id: str,
    name: str,
) -> Dict[str, str]:
    """优先 content_file（单 md）；否则兼容旧 content_files 三文件。"""
    content_file = data.get("content_file")
    if content_file:
        raw = _read_path_under_root(
            templates_root,
            template_id=template_id,
            label="content_file",
            relative_path=content_file,
        )
        parts = split_unified_template_md(raw)
        if parts:
            return parts
        return {
            "guidance": f"模板：{name}",
            "preview": raw,
            "generation": raw,
        }

    content_files = data.get("content_files")
    if not isinstance(content_files, dict):
        raise TemplateConfigurationError(
            f"模板 {template_id} 须配置 content_file 或 content_files"
        )
    missing = [key for key in _CONTENT_KEYS if not content_files.get(key)]
    if missing:
        raise TemplateConfigurationError(
            f"模板 {template_id} 缺少内容文件配置: {', '.join(missing)}"
        )
    return {
        key: _read_content_file(
            templates_root,
            template_id=template_id,
            content_type=key,
            relative_path=content_files[key],
        )
        for key in _CONTENT_KEYS
    }


def _parse_template(templates_root: Path, data: Dict[str, Any]) -> ReportTemplate:
    template_id = str(data.get("id") or "").strip()
    if not template_id:
        raise TemplateConfigurationError("模板条目缺少 id")

    try:
        max_output_tokens = int(data.get("max_output_tokens"))
    except (TypeError, ValueError) as exc:
        raise TemplateConfigurationError(
            f"模板 {template_id} 的 max_output_tokens 必须是整数"
        ) from exc
    if not _MIN_OUTPUT_TOKENS <= max_output_tokens <= _MAX_OUTPUT_TOKENS:
        raise TemplateConfigurationError(
            f"模板 {template_id} 的 max_output_tokens 必须在 "
            f"{_MIN_OUTPUT_TOKENS}～{_MAX_OUTPUT_TOKENS} 之间"
        )

    name = str(data.get("name") or template_id)
    contents = _load_contents(
        templates_root, data, template_id=template_id, name=name
    )
    return ReportTemplate(
        id=template_id,
        name=name,
        tagline=str(data.get("tagline") or ""),
        length_label=str(data.get("length_label") or ""),
        tags=[str(tag) for tag in (data.get("tags") or [])],
        recommended=bool(data.get("recommended", False)),
        sort_order=int(data.get("sort_order") or 0),
        max_output_tokens=max_output_tokens,
        guidance_content=contents["guidance"],
        preview_content=contents["preview"],
        generation_content=contents["generation"],
    )


def list_template_ids(project_root: Optional[Path] = None) -> List[str]:
    manifest = _load_manifest(project_root)
    entries = sorted(
        manifest["templates"],
        key=lambda entry: int(entry.get("sort_order") or 0),
    )
    return [str(entry["id"]) for entry in entries if entry.get("id")]


def load_builtin_report_templates(
    *,
    project_root: Optional[Path] = None,
) -> List[ReportTemplate]:
    """仅加载镜像内置 report_templates/（不含全局自定义）。"""
    root = _templates_root(project_root)
    manifest = _load_manifest(project_root)
    entries = sorted(
        manifest["templates"],
        key=lambda entry: int(entry.get("sort_order") or 0),
    )
    templates: List[ReportTemplate] = []
    for entry in entries:
        if not entry.get("id"):
            continue
        try:
            templates.append(_parse_template(root, entry))
        except Exception as exc:
            logger.warning(f"跳过模板 {entry.get('id')!r}: {exc}")
    return templates


def load_builtin_report_template(
    template_id: Optional[str] = None,
    *,
    project_root: Optional[Path] = None,
) -> ReportTemplate:
    """仅从镜像内置清单加载单个模板。"""
    root = _templates_root(project_root)
    manifest = _load_manifest(project_root)
    wanted = str(
        template_id or manifest.get("default_template_id") or _DEFAULT_TEMPLATE_ID
    ).strip()
    entry = next(
        (item for item in manifest["templates"] if item.get("id") == wanted),
        None,
    )
    if entry is None:
        raise TemplateConfigurationError(f"未知报告模板: {wanted}")
    return _parse_template(root, entry)


def load_all_report_templates(
    *,
    project_root: Optional[Path] = None,
    session_id: Optional[str] = None,
) -> List[ReportTemplate]:
    """Load templates for mode-1 list / event2：系统 ∪ 全局自定义。

    ``session_id`` 已忽略（模板操作与会话无关）；保留参数仅为兼容旧调用。
    """
    del session_id
    from singleAgent.session_templates import load_merged_report_templates

    return load_merged_report_templates(project_root=project_root)


def load_report_template(
    template_id: Optional[str] = None,
    *,
    project_root: Optional[Path] = None,
    session_id: Optional[str] = None,
) -> ReportTemplate:
    del session_id
    from singleAgent.session_templates import load_merged_report_template

    return load_merged_report_template(template_id, project_root=project_root)


def resolve_ismodel(metadata: Optional[Dict[str, Any]] = None) -> int:
    """Resolve the independent frontend template-list switch (1=on, 2=off)."""
    meta = metadata or {}
    raw = meta.get("ismodel")
    if raw is None:
        raw = meta.get("isModel")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 2
    return 1 if value == 1 else 2


def resolve_template_id(metadata: Optional[Dict[str, Any]] = None) -> str:
    """从 metadata / ext 解析模板 id；remote 未选时返回空串由合并加载器挑默认。"""
    from singleAgent.config import agent_template_source

    meta = metadata or {}
    for key in ("report_template", "selected_template_id", "template_id"):
        value = meta.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()

    ext = meta.get("ext")
    if isinstance(ext, dict):
        ids = ext.get("selected_knowledge_ids") or ext.get("selectedKnowledgeIds")
        if isinstance(ids, (list, tuple)) and ids:
            first = ids[0]
            if first is not None and str(first).strip():
                return str(first).strip()
        for key in ("report_template", "template_id", "selected_template_id"):
            value = ext.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()

    if agent_template_source() == "remote":
        return ""
    return _DEFAULT_TEMPLATE_ID
