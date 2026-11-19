"""全局报告模板增删改 API（与 session 无关）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, Body, Path as FPath, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from singleAgent.session_templates import (
    SessionTemplateError,
    create_template,
    delete_template,
    get_template,
    list_templates,
    restore_template,
    update_template,
)

router = APIRouter(tags=["ReportTemplates"])

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TemplateUpsertBody(BaseModel):
    id: Optional[str] = Field(
        None, description="模板 slug；创建时可省略，后端生成 custom_xxxxxxxx"
    )
    name: str = Field(..., description="展示名称")
    tagline: str = ""
    length_label: str = ""
    max_output_tokens: int = 1400
    recommended: bool = False
    sort_order: int = 100
    tags: List[str] = Field(default_factory=list)
    # 推荐：一份完整 Markdown（自由格式）
    content: Optional[str] = Field(
        None, description="完整模板 Markdown；有则优先，无需再拆三段"
    )
    # 兼容旧三段（可选）
    guidance: Optional[str] = Field(None, description="可选；仅模型用")
    preview: Optional[str] = Field(None, description="可选；右栏展示")
    generation: Optional[str] = Field(None, description="可选；仅模型用")


class TemplateUpdateBody(BaseModel):
    name: Optional[str] = None
    tagline: Optional[str] = None
    length_label: Optional[str] = None
    max_output_tokens: Optional[int] = None
    recommended: Optional[bool] = None
    sort_order: Optional[int] = None
    tags: Optional[List[str]] = None
    content: Optional[str] = Field(
        None, description="完整模板 Markdown；有则整份覆盖"
    )
    guidance: Optional[str] = None
    preview: Optional[str] = None
    generation: Optional[str] = None


def _ok(data: Any, msg: str = "success", code: int = 0) -> JSONResponse:
    return JSONResponse(content={"code": code, "msg": msg, "data": data})


def _err(status: int, msg: str, *, code: int = 0) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"code": code or status, "msg": msg, "detail": msg, "data": None},
    )


@router.get("/api/v1/report-templates")
async def api_list_templates(
    include_content: bool = Query(
        True,
        description="是否返回 guidance/preview/generation；列表管理建议 true",
    ),
):
    """列出可见模板（系统内置 ∪ 全局自定义，已隐藏系统模板除外）。"""
    try:
        items = list_templates(
            project_root=_PROJECT_ROOT,
            include_content=include_content,
        )
    except SessionTemplateError as e:
        return _err(400, str(e), code=40001)
    return _ok({"total": len(items), "items": items})


@router.get("/api/v1/report-templates/{template_id}")
async def api_get_template(template_id: str = FPath(..., description="模板 slug")):
    try:
        item = get_template(template_id, project_root=_PROJECT_ROOT)
    except SessionTemplateError as e:
        return _err(404, str(e), code=40401)
    return _ok(item)


@router.post("/api/v1/report-templates")
async def api_create_template(body: TemplateUpsertBody = Body(...)):
    """新增自定义模板。不可占用系统 slug（覆盖请用 PUT）。"""
    try:
        item = create_template(body.model_dump(), project_root=_PROJECT_ROOT)
    except SessionTemplateError as e:
        return _err(400, str(e), code=40001)
    return _ok(item)


@router.put("/api/v1/report-templates/{template_id}")
async def api_update_template(
    template_id: str,
    body: TemplateUpdateBody = Body(...),
):
    """更新模板；若 template_id 为系统模板，则写入全局覆盖副本。"""
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    if not payload:
        return _err(400, "请求体不能为空", code=40001)
    try:
        item = update_template(template_id, payload, project_root=_PROJECT_ROOT)
    except SessionTemplateError as e:
        msg = str(e)
        status = 404 if "不存在" in msg else 400
        return _err(status, msg, code=40401 if status == 404 else 40001)
    return _ok(item)


@router.delete("/api/v1/report-templates/{template_id}")
async def api_delete_template(template_id: str):
    """
    删除：
    - 自定义 / 系统覆盖：删除文件，系统原版恢复可见
    - 纯系统模板：软隐藏
    """
    try:
        result = delete_template(template_id, project_root=_PROJECT_ROOT)
    except SessionTemplateError as e:
        return _err(404, str(e), code=40401)
    return _ok(result)


@router.post("/api/v1/report-templates/{template_id}/restore")
async def api_restore_template(template_id: str):
    """恢复被软隐藏的系统模板（并清除覆盖副本）。"""
    try:
        item = restore_template(template_id, project_root=_PROJECT_ROOT)
    except SessionTemplateError as e:
        return _err(400, str(e), code=40001)
    return _ok(item)
