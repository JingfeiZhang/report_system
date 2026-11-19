"""事件详情 / 对外开放列表等接口（open/events 代理上游；sensitive/impact 接达梦）。"""

from fastapi import APIRouter

from event_report.impact import router as impact_router
from event_report.open_events import router as open_events_router
from event_report.sensitive_analysis import router as sensitive_analysis_router

router = APIRouter()
router.include_router(impact_router)
router.include_router(open_events_router)
router.include_router(sensitive_analysis_router)

__all__ = ["router"]
