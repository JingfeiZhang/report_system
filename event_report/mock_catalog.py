"""7-31 三接口共享静态 mock 目录。

默认演示窗口：2026-08-06～2026-08-20（敏感日历 + 公开态势）。
旧 ID（171114 / 101 / 1 等）保留，供详情直链兼容；默认列表不展示窗外事件。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

# 前端约定：不传日期时落在此窗
DEFAULT_WINDOW_START = "2026-08-06"
DEFAULT_WINDOW_END = "2026-08-20"


def _trend(
    event_date: str,
    *,
    peak: int,
    before_days: int = 5,
    after_days: int = 3,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    """生成 heat_trend + observation_window + heat 摘要。"""
    ed = datetime.strptime(event_date, "%Y-%m-%d")
    start = ed - timedelta(days=before_days)
    end = ed + timedelta(days=after_days)
    # 简单爬坡：事件日为 peak，前后递减
    points: List[Dict[str, Any]] = []
    for offset in range(-before_days, after_days + 1):
        d = ed + timedelta(days=offset)
        if offset < 0:
            heat = max(80, int(peak * (0.28 + 0.12 * (before_days + offset))))
        elif offset == 0:
            heat = peak
        else:
            heat = max(60, int(peak * (0.9 - 0.18 * offset)))
        points.append(
            {
                "date": d.strftime("%Y-%m-%d"),
                "offset_day": offset,
                "heat": heat,
                "is_event_day": offset == 0,
            }
        )
    prev = next((p["heat"] for p in points if p["offset_day"] == -1), None)
    change_rate = None
    direction = "unknown"
    if prev and prev > 0:
        change_rate = round((peak - prev) / prev * 100, 1)
        direction = "up" if change_rate > 0 else ("down" if change_rate < 0 else "flat")
    heat = {
        "total": peak,
        "comparison_base": "previous_day",
        "previous_value": prev,
        "change_rate": change_rate,
        "change_direction": direction,
    }
    window = {
        "before_days": before_days,
        "after_days": after_days,
        "start_date": start.strftime("%Y-%m-%d"),
        "event_date": event_date,
        "end_date": end.strftime("%Y-%m-%d"),
        "status": "active",
    }
    return points, window, heat


def _opinions(
    rows: List[Tuple[str, str, str, int, str]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, (account, platform, content, heat, published) in enumerate(rows, 1):
        out.append(
            {
                "rank": i,
                "account_id": f"media_{10000 + i}",
                "account_name": account,
                "platform": platform,
                "content": content,
                "interaction_heat": heat,
                "published_at": published,
                "source_url": f"https://example.com/post/{10000 + i}",
            }
        )
    return out


def _list_item(
    *,
    eid: int,
    title: str,
    event_date: str,
    place_name: str,
    region: str,
    topic_id: str,
    category_id: int,
    summary: str,
    is_sensitive: bool,
    llm_enhanced: bool = True,
    gdelt_count: Optional[int] = None,
    total_mentions: Optional[int] = None,
    data_source: Optional[str] = None,
    cover_url: Optional[str] = None,
    event_type: Optional[str] = None,
) -> Dict[str, Any]:
    """open/events 列表项。

    与 is_sensitive 绑定（互斥）：
    - false → event_type=monitor，id=main_event_id，record_event_id=null
    - true  → event_type=record，id=record_event_id，main_event_id=null

    ``event_type`` 入参仅兼容旧调用；实际以 ``is_sensitive`` 为准。
    """
    et = "record" if is_sensitive else "monitor"
    if event_type is not None and str(event_type).strip() and str(event_type).strip() != et:
        # 忽略冲突入参，避免再出现「敏感却标 monitor」
        pass
    item: Dict[str, Any] = {
        "event_type": et,
        "id": eid,
        "main_event_id": eid if not is_sensitive else None,
        "record_event_id": eid if is_sensitive else None,
        "title": title,
        "event_date": event_date,
        "place_name": place_name,
        "region": region,
        "topic_id": topic_id,
        "category_id": category_id,
        "summary": summary,
        "is_sensitive": is_sensitive,
        "data_source": data_source,
        "cover_url": cover_url,
        "llm_enhanced": llm_enhanced,
        "gdelt_count": gdelt_count,
        "total_mentions": total_mentions,
    }
    return item


def _sa_block(
    event_date: str,
    *,
    peak: int,
    opinions: List[Tuple[str, str, str, int, str]],
    same_period: Optional[List[Dict[str, Any]]] = None,
    window_status: str = "active",
) -> Dict[str, Any]:
    trend, window, heat = _trend(event_date, peak=peak)
    window["status"] = window_status
    y = int(event_date[:4])
    if same_period is None:
        mmdd = event_date[5:]  # MM-DD
        same_period = [
            {
                "year": y - 1,
                "heat": int(peak * 0.72),
                "period_start": f"{y - 1}-{mmdd}",
                "period_end": f"{y - 1}-{mmdd}",
            },
            {
                "year": y - 2,
                "heat": int(peak * 0.58),
                "period_start": f"{y - 2}-{mmdd}",
                "period_end": f"{y - 2}-{mmdd}",
            },
        ]
    return {
        "event_date": event_date,
        "heat": heat,
        "same_period_comparison": same_period,
        "observation_window": window,
        "heat_trend": trend,
        "self_media_opinions": _opinions(opinions[:3]),
    }


def _impact_block(
    main_event_id: int,
    *,
    impact_scope: str,
    affected_population: str,
    casualties: str,
    economic_loss: str,
    resolution_result: str,
    avg_goldstein: float,
    avg_tone: float,
    word_cloud: List[Dict[str, Any]],
    media_opinions: Dict[str, List[Dict[str, Any]]],
    updated_at: str,
) -> Dict[str, Any]:
    return {
        "main_event_id": main_event_id,
        "impact_scope": impact_scope,
        "affected_population": affected_population,
        "casualties": casualties,
        "economic_loss": economic_loss,
        "resolution_result": resolution_result,
        "avg_goldstein": avg_goldstein,
        "avg_tone": avg_tone,
        "word_cloud": word_cloud,
        "media_opinions": media_opinions,
        "updated_at": updated_at,
    }


def _normalize_event_date(event_date: str, default: str = "2026-08-06") -> str:
    ed = (event_date or "").strip()[:10]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", ed):
        return ed
    return default


def _stable_demo_scores(event_id: Union[int, str]) -> tuple[float, float]:
    """同一事件 ID 固定、不同 ID 有差异的 avg_goldstein / avg_tone。"""
    digest = hashlib.md5(str(event_id).strip().encode("utf-8")).hexdigest()
    a = int(digest[0:2], 16)
    b = int(digest[2:4], 16)
    # Goldstein 约 -7.5～-0.5（偏冲突，对应前端「事件热度」）
    goldstein = round(-7.5 + (a / 255.0) * 7.0, 1)
    # Tone 约 -4.0～1.0（情感有正有负）
    tone = round(-4.0 + (b / 255.0) * 5.0, 1)
    return goldstein, tone


def _stable_word_cloud_base(event_id: Union[int, str]) -> int:
    seed = int(hashlib.md5(f"wc:{event_id}".encode("utf-8")).hexdigest()[:4], 16)
    return 70 + (seed % 25)  # 70..94

def build_demo_sensitive(
    event_id: Union[int, str],
    *,
    event_type: str = "monitor",
    title: str = "",
    event_date: str = "2026-08-06",
) -> Dict[str, Any]:
    """无库兜底灌满：按 event_date 重算曲线；文案保持真实舆情口吻（不出现「演示」字样）。"""
    ed = _normalize_event_date(event_date)
    label = (title or "").strip() or f"事件{event_id}"
    t0 = f"{ed}T09:20:00+08:00"
    t1 = f"{ed}T11:40:00+08:00"
    t2 = f"{ed}T15:10:00+08:00"
    block = _sa_block(
        ed,
        peak=1280,
        opinions=[
            (
                "舆情观察家",
                "微博",
                "公开讨论明显升温，建议同步权威通报并关注情绪极化与谣言扩散风险。",
                920,
                t0,
            ),
            (
                "理性看世界",
                "知乎",
                "多方解读并存，需持续跟踪境外镜像传播与处置进展对舆论的影响。",
                710,
                t1,
            ),
            (
                "一线观察",
                "小红书",
                "网民关注点集中在后续处置与次生舆情，宜提前准备口径与回应节奏。",
                540,
                t2,
            ),
        ],
    )
    block["title"] = label
    block["source"] = "demo_fallback"
    block["event_type"] = event_type
    return block


def build_demo_impact(
    main_event_id: Union[int, str],
    *,
    title: str = "",
    event_date: str = "2026-08-06",
) -> Dict[str, Any]:
    """无库兜底 impact：结构化字段饱满；文案与信源外观贴近真实舆情。"""
    ed = _normalize_event_date(event_date)
    label = (title or "").strip() or f"事件{main_event_id}"
    try:
        mid: Union[int, str] = int(str(main_event_id).strip())
    except (TypeError, ValueError):
        mid = main_event_id
    mid_int = mid if isinstance(mid, int) else 0
    short = label[:12] if label else "相关事件"
    goldstein, tone = _stable_demo_scores(mid)
    w_base = _stable_word_cloud_base(mid)
    block = _impact_block(
        mid_int,
        impact_scope="区域舆情与社会关注面",
        affected_population="相关地区公众及相关行业从业者",
        casualties="暂无权威伤亡数据，仍按公开口径持续核验",
        economic_loss="暂无完整量化损失评估，需结合后续权威通报更新",
        resolution_result="建议持续监测、协同口径，并预置次生风险处置清单",
        avg_goldstein=goldstein,
        avg_tone=tone,
        word_cloud=[
            {"text": "舆情监测", "weight": min(99, w_base + 8), "category": "topic"},
            {"text": "风险研判", "weight": min(99, w_base + 2), "category": "topic"},
            {"text": "权威通报", "weight": max(40, w_base - 4), "category": "source"},
            {"text": "次生舆情", "weight": max(35, w_base - 10), "category": "risk"},
            {"text": "舆论引导", "weight": max(30, w_base - 16), "category": "response"},
            {"text": short, "weight": max(28, w_base - 20), "category": "event"},
        ],
        media_opinions={
            "domestic": [
                {
                    "source": "舆情观察",
                    "view": "公开讨论升温，宜同步权威信息与处置进展，避免信息真空。",
                    "sentiment_score": 62,
                    "sentiment_label": "关注",
                    "url": "https://weibo.com/ttarticle/p/show?id=23094001",
                    "published_at": f"{ed}T10:00:00+08:00",
                },
                {
                    "source": "态势速览",
                    "view": "需警惕情绪极化与不实信息扩散，建议加强核查与回应节奏。",
                    "sentiment_score": 48,
                    "sentiment_label": "警惕",
                    "url": "https://www.zhihu.com/question/650000001",
                    "published_at": f"{ed}T12:30:00+08:00",
                },
            ],
            "international": [
                {
                    "source": "Reuters",
                    "view": "Regional attention is rising; markets and media await official updates.",
                    "sentiment_score": 45,
                    "sentiment_label": "中性",
                    "url": "https://www.reuters.com/world/",
                    "published_at": f"{ed}T09:40:00+08:00",
                }
            ],
        },
        updated_at=f"{ed}T16:00:00+08:00",
    )
    block["main_event_id"] = mid
    block["title"] = label
    block["event_date"] = ed
    block["source"] = "demo_fallback"
    return block


# —— 默认窗口内事件（列表主数据）——
_WINDOW_LIST: List[Dict[str, Any]] = [
    _list_item(
        eid=20260806,
        title="广岛原子弹爆炸纪念日相关舆情升温",
        event_date="2026-08-06",
        place_name="广岛",
        region="foreign",
        topic_id="history_memory",
        category_id=752,
        summary="8月6日广岛原爆纪念日前后，国际反核与历史记忆议题讨论升温，多国媒体跟进公祭与纪念活动。",
        is_sensitive=True,
        gdelt_count=960,
        total_mentions=6420,
    ),
    _list_item(
        eid=20260807,
        title="沙特土耳其巴基斯坦签署麦加联合防务安排",
        event_date="2026-08-07",
        place_name="麦加",
        region="foreign",
        topic_id="middle_east",
        category_id=752,
        summary="公开报道称沙特、土耳其、巴基斯坦在麦加达成联合防务/威慑安排，地区安全格局引发广泛关注。",
        is_sensitive=True,
        gdelt_count=1480,
        total_mentions=9120,
    ),
    _list_item(
        eid=20260808,
        title="中东局势外溢与海湾安全风险升温",
        event_date="2026-08-08",
        place_name="海湾地区",
        region="foreign",
        topic_id="middle_east",
        category_id=752,
        summary="多方公开报道指地区冲突外溢、航运与能源通道不确定性上升，市场与舆论同步关注。",
        is_sensitive=True,
        gdelt_count=1320,
        total_mentions=8050,
    ),
    # 旧样例迁入窗口，保留 id=171114 兼容详情直链
    _list_item(
        eid=171114,
        title="美伊以冲突相关态势持续发酵",
        event_date="2026-08-09",
        place_name="中东",
        region="foreign",
        topic_id="middle_east",
        category_id=752,
        summary="地区局势紧张，多方表态关注能源、航运与停火斡旋进展。",
        is_sensitive=True,
        gdelt_count=1280,
        total_mentions=8642,
    ),
    _list_item(
        eid=20260811,
        title="中菲黄岩岛附近海域舰艇摩擦受关注",
        event_date="2026-08-11",
        place_name="黄岩岛附近海域",
        region="foreign",
        topic_id="south_china_sea",
        category_id=752,
        summary="敏感日历节点前后，中菲在黄岩岛附近海域舰艇摩擦相关公开信息引发舆论跟进与外交表态。",
        is_sensitive=True,
        gdelt_count=1180,
        total_mentions=7360,
    ),
    _list_item(
        eid=20260815,
        title="日本宣布投降纪念日舆论高峰",
        event_date="2026-08-15",
        place_name="东京",
        region="foreign",
        topic_id="history_memory",
        category_id=752,
        summary="8月15日终战日前后，历史叙事、纪念活动与地区安全话题在多语种舆论场形成高峰。",
        is_sensitive=True,
        gdelt_count=1020,
        total_mentions=6880,
    ),
    _list_item(
        eid=20260818,
        title="亚太经贸合作论坛筹备进展",
        event_date="2026-08-18",
        place_name="新加坡",
        region="foreign",
        topic_id="global_governance",
        category_id=100,
        summary="非敏感对照样例：区域经贸合作论坛筹备与议程讨论，用于 is_sensitive=false 筛选验证。",
        is_sensitive=False,
        gdelt_count=210,
        total_mentions=980,
        llm_enhanced=True,
    ),
]

# —— 窗外旧样例（仅显式扩窗 / 详情直链；默认列表不出现）——
_LEGACY_LIST: List[Dict[str, Any]] = [
    _list_item(
        eid=101,
        title="基辅遭袭",
        event_date="2024-01-02",
        place_name="基辅",
        region="foreign",
        topic_id="russia_ukraine",
        category_id=752,
        summary="俄军对基辅发动空袭，多地响起防空警报。",
        is_sensitive=True,
        gdelt_count=520,
        total_mentions=3100,
    ),
    _list_item(
        eid=1,
        title="四通桥事件",
        event_date="2022-10-13",
        place_name="北京",
        region="domestic",
        topic_id="cn_us_jp_kr",
        category_id=752,
        summary="记录线样例事件（兼容旧前端直链）。",
        is_sensitive=True,
        data_source="wiki",
        cover_url="/api/v1/event-report/cover?event_type=record&record_event_id=1",
        llm_enhanced=False,
    ),
    _list_item(
        eid=202,
        title="G20 峰会筹备进展",
        event_date="2025-11-10",
        place_name="里约",
        region="foreign",
        topic_id="global_governance",
        category_id=100,
        summary="非敏感监测样例（窗外，扩窗可见）。",
        is_sensitive=False,
        gdelt_count=88,
        total_mentions=420,
    ),
    _list_item(
        eid=303,
        title="国内某地交通枢纽扩建通车",
        event_date="2025-06-01",
        place_name="上海",
        region="domestic",
        topic_id="cn_infra",
        category_id=200,
        summary="国内非敏感样例（窗外）。",
        is_sensitive=False,
        gdelt_count=12,
        total_mentions=56,
        llm_enhanced=False,
    ),
]

MOCK_LIST_ITEMS: List[Dict[str, Any]] = list(_WINDOW_LIST) + list(_LEGACY_LIST)

# —— sensitive-analysis ——
MOCK_SENSITIVE_MONITOR: Dict[str, Dict[str, Any]] = {
    "20260806": _sa_block(
        "2026-08-06",
        peak=6420,
        opinions=[
            (
                "@历史观察",
                "微博",
                "广岛公祭活动引国际跟进，反核与和平议题再次成为焦点。",
                2860,
                "2026-08-06T09:10:00+08:00",
            ),
            (
                "@国际速览",
                "微信公众号",
                "多国政要发文纪念，强调避免核冲突重演。",
                2140,
                "2026-08-06T10:05:00+08:00",
            ),
            (
                "@东亚时评",
                "抖音",
                "历史记忆与当下地区安全叙事交织，舆论分层明显。",
                1760,
                "2026-08-06T11:20:00+08:00",
            ),
        ],
    ),
    "20260807": _sa_block(
        "2026-08-07",
        peak=9120,
        opinions=[
            (
                "@中东观察员",
                "微博",
                "麦加防务安排被解读为地区安全架构调整的信号。",
                3520,
                "2026-08-07T14:30:00+08:00",
            ),
            (
                "@战略雷达",
                "微信公众号",
                "公开通稿强调防御与稳定，市场关注能源通道风险。",
                2680,
                "2026-08-07T15:10:00+08:00",
            ),
            (
                "@外媒编译",
                "抖音",
                "外媒聚焦三国合作动机及对现有安全同盟的影响。",
                2210,
                "2026-08-07T16:00:00+08:00",
            ),
        ],
    ),
    "20260808": _sa_block(
        "2026-08-08",
        peak=8050,
        opinions=[
            (
                "@航运情报",
                "微博",
                "海湾航运保险成本波动，业界关注冲突外溢。",
                3010,
                "2026-08-08T08:40:00+08:00",
            ),
            (
                "@能源市场",
                "微信公众号",
                "油价与避险情绪同步抬升，短期波动加大。",
                2440,
                "2026-08-08T09:25:00+08:00",
            ),
            (
                "@国际观察",
                "抖音",
                "多方呼吁保持克制，避免误判升级。",
                1980,
                "2026-08-08T10:15:00+08:00",
            ),
        ],
    ),
    "171114": _sa_block(
        "2026-08-09",
        peak=8642,
        opinions=[
            (
                "@热点观察员",
                "微博",
                "地区冲突持续牵动能源与航运，关注停火斡旋进展。",
                3246,
                "2026-08-09T09:20:00+08:00",
            ),
            (
                "@民生视角",
                "微博",
                "人道通道与平民安全仍是舆论核心关切。",
                2418,
                "2026-08-09T09:35:00+08:00",
            ),
            (
                "@城市记录者",
                "微信公众号",
                "国际组织呼吁降温止损，避免外溢风险扩大。",
                1876,
                "2026-08-09T09:50:00+08:00",
            ),
        ],
        same_period=[
            {
                "year": 2025,
                "heat": 5980,
                "period_start": "2025-08-04",
                "period_end": "2025-08-12",
            },
            {
                "year": 2024,
                "heat": 5038,
                "period_start": "2024-08-04",
                "period_end": "2024-08-12",
            },
            {
                "year": 2023,
                "heat": 4036,
                "period_start": "2023-08-04",
                "period_end": "2023-08-12",
            },
        ],
    ),
    "20260811": _sa_block(
        "2026-08-11",
        peak=7360,
        opinions=[
            (
                "@南海瞭望",
                "微博",
                "黄岩岛附近海域摩擦信息发酵，外交表态密集。",
                3180,
                "2026-08-11T11:05:00+08:00",
            ),
            (
                "@海事速递",
                "微信公众号",
                "公开画面与通报引发对航行安全与局势管控的讨论。",
                2520,
                "2026-08-11T12:40:00+08:00",
            ),
            (
                "@区域时评",
                "抖音",
                "各方强调依据国际法处理争议，避免擦枪走火。",
                2010,
                "2026-08-11T13:20:00+08:00",
            ),
        ],
    ),
    "20260815": _sa_block(
        "2026-08-15",
        peak=6880,
        opinions=[
            (
                "@历史记忆",
                "微博",
                "终战日纪念活动集中发布，历史叙事竞争升温。",
                2950,
                "2026-08-15T09:00:00+08:00",
            ),
            (
                "@东亚观察",
                "微信公众号",
                "纪念与现实安全议题交织，多语种舆论场分化。",
                2330,
                "2026-08-15T10:30:00+08:00",
            ),
            (
                "@国际编译",
                "抖音",
                "外媒关注地区纪念活动与领导人表态。",
                1890,
                "2026-08-15T11:45:00+08:00",
            ),
        ],
    ),
    "20260818": _sa_block(
        "2026-08-18",
        peak=980,
        opinions=[
            (
                "@经贸观察",
                "微博",
                "论坛筹备进展平稳，议程聚焦供应链与绿色投资。",
                420,
                "2026-08-18T10:00:00+08:00",
            ),
            (
                "@亚太商讯",
                "微信公众号",
                "企业界关注关税与物流便利化议题。",
                310,
                "2026-08-18T11:20:00+08:00",
            ),
        ],
    ),
    "101": _sa_block(
        "2024-01-02",
        peak=3100,
        opinions=[
            (
                "@战地速览",
                "微博",
                "防空警报拉响后市民疏散有序，关注后续设施受损评估。",
                2100,
                "2024-01-02T08:15:00+08:00",
            ),
            (
                "@国际观察",
                "抖音",
                "多方呼吁避免民用目标受波及，人道通道需保持畅通。",
                1650,
                "2024-01-02T09:00:00+08:00",
            ),
        ],
        window_status="finished",
    ),
}

MOCK_SENSITIVE_RECORD: Dict[str, Dict[str, Any]] = {
    "1": {
        "event_date": "2022-10-13",
        "heat": {
            "total": 0,
            "comparison_base": "previous_day",
            "previous_value": None,
            "change_rate": None,
            "change_direction": "unknown",
        },
        "same_period_comparison": [],
        "observation_window": {
            "before_days": 5,
            "after_days": 3,
            "start_date": "2022-10-08",
            "event_date": "2022-10-13",
            "end_date": "2022-10-16",
            "status": "finished",
        },
        "heat_trend": [],
        "self_media_opinions": [],
    },
}

# 敏感列表项统一为 record 线：展开详情按 event_type=record 必须能取到热度。
# 非敏感对照 20260818 仅保留在 MONITOR，供旧 monitor 直链兼容。
for _sid, _block in MOCK_SENSITIVE_MONITOR.items():
    if _sid == "20260818":
        continue
    MOCK_SENSITIVE_RECORD.setdefault(_sid, _block)

# —— impact（监测线）——
MOCK_IMPACT: Dict[str, Dict[str, Any]] = {
    "171114": _impact_block(
        171114,
        impact_scope="全球",
        affected_population="约 260 万人受到直接或间接影响",
        casualties="死亡 127 人、受伤 438 人；数字仍在核实",
        economic_loss="国际油价短期上涨约 4.2%，航运与保险成本上升",
        resolution_result="有关各方仍在斡旋，暂未形成稳定停火安排",
        avg_goldstein=-7.8,
        avg_tone=-4.2,
        word_cloud=[
            {"text": "停火协议", "weight": 96, "category": "diplomacy"},
            {"text": "地区安全", "weight": 88, "category": "conflict"},
            {"text": "人道援助", "weight": 75, "category": "humanitarian"},
            {"text": "国际谴责", "weight": 72, "category": "diplomacy"},
            {"text": "原油价格", "weight": 68, "category": "economy"},
            {"text": "难民安置", "weight": 61, "category": "humanitarian"},
            {"text": "军事部署", "weight": 58, "category": "conflict"},
            {"text": "联合国斡旋", "weight": 54, "category": "diplomacy"},
        ],
        media_opinions={
            "domestic": [
                {
                    "source": "新华社",
                    "view": "呼吁有关方面保持克制，通过对话解决分歧。",
                    "sentiment_score": 85,
                    "sentiment_label": "批判",
                    "url": "https://example.com/news/1",
                    "published_at": "2026-08-09T09:20:00+08:00",
                },
                {
                    "source": "人民日报",
                    "view": "地区局势牵动全球能源与航运，应尽快降温止损。",
                    "sentiment_score": 78,
                    "sentiment_label": "担忧",
                    "url": "https://example.com/news/3",
                    "published_at": "2026-08-09T11:00:00+08:00",
                },
            ],
            "international": [
                {
                    "source": "Reuters",
                    "view": "事件加剧地区不确定性，市场持续关注各方后续行动。",
                    "sentiment_score": 72,
                    "sentiment_label": "担忧",
                    "url": "https://example.com/news/2",
                    "published_at": "2026-08-09T10:00:00+08:00",
                },
                {
                    "source": "AP",
                    "view": "人道组织警告冲突升级将进一步推高流离失所人数。",
                    "sentiment_score": 80,
                    "sentiment_label": "强烈批判",
                    "url": "https://example.com/news/4",
                    "published_at": "2026-08-09T12:15:00+08:00",
                },
            ],
        },
        updated_at="2026-08-09T10:30:00+08:00",
    ),
    "20260806": _impact_block(
        20260806,
        impact_scope="国际舆论场",
        affected_population="主要体现为纪念活动参与者与全球舆论受众，无直接伤亡统计口径",
        casualties="暂无权威伤亡数据（纪念节点）",
        economic_loss="暂无直接经济损失量化；局部文旅与安保投入上升",
        resolution_result="各国按惯例举行纪念与公祭活动，议题仍在发酵",
        avg_goldstein=-2.1,
        avg_tone=-1.8,
        word_cloud=[
            {"text": "反核", "weight": 92, "category": "diplomacy"},
            {"text": "和平", "weight": 88, "category": "diplomacy"},
            {"text": "历史记忆", "weight": 84, "category": "society"},
            {"text": "广岛", "weight": 80, "category": "conflict"},
            {"text": "公祭", "weight": 70, "category": "society"},
        ],
        media_opinions={
            "domestic": [
                {
                    "source": "新华社",
                    "view": "铭记历史、珍爱和平，坚定维护国际核安全秩序。",
                    "sentiment_score": 70,
                    "sentiment_label": "中性偏正",
                    "url": "https://example.com/news/h1",
                    "published_at": "2026-08-06T09:00:00+08:00",
                }
            ],
            "international": [
                {
                    "source": "NHK",
                    "view": "广岛举行和平纪念仪式，呼吁实现无核武世界。",
                    "sentiment_score": 65,
                    "sentiment_label": "中性",
                    "url": "https://example.com/news/h2",
                    "published_at": "2026-08-06T08:30:00+08:00",
                }
            ],
        },
        updated_at="2026-08-06T12:00:00+08:00",
    ),
    "20260807": _impact_block(
        20260807,
        impact_scope="中东及周边",
        affected_population="主要影响地区安全预期与能源市场情绪，人口影响待评估",
        casualties="暂无权威伤亡数据",
        economic_loss="避险情绪推高能源与航运相关波动",
        resolution_result="协议文本与后续落实仍待各方进一步说明",
        avg_goldstein=-5.2,
        avg_tone=-3.1,
        word_cloud=[
            {"text": "联合防务", "weight": 95, "category": "conflict"},
            {"text": "麦加", "weight": 90, "category": "diplomacy"},
            {"text": "地区安全", "weight": 86, "category": "conflict"},
            {"text": "能源通道", "weight": 74, "category": "economy"},
            {"text": "同盟关系", "weight": 68, "category": "diplomacy"},
        ],
        media_opinions={
            "domestic": [
                {
                    "source": "环球时报",
                    "view": "地区国家寻求自主安全安排，反映现有秩序承压。",
                    "sentiment_score": 76,
                    "sentiment_label": "担忧",
                    "url": "https://example.com/news/m1",
                    "published_at": "2026-08-07T16:00:00+08:00",
                }
            ],
            "international": [
                {
                    "source": "Al Jazeera",
                    "view": "三国签署联合防务安排，分析指与地区冲突背景相关。",
                    "sentiment_score": 74,
                    "sentiment_label": "担忧",
                    "url": "https://example.com/news/m2",
                    "published_at": "2026-08-07T15:20:00+08:00",
                }
            ],
        },
        updated_at="2026-08-07T18:00:00+08:00",
    ),
    "20260808": _impact_block(
        20260808,
        impact_scope="海湾及国际航运",
        affected_population="航运从业者与能源进口依赖经济体间接受影响",
        casualties="暂无权威伤亡数据",
        economic_loss="航运保险与燃油附加成本抬升",
        resolution_result="各方呼吁降温，局势仍不稳定",
        avg_goldstein=-6.4,
        avg_tone=-3.8,
        word_cloud=[
            {"text": "航运风险", "weight": 93, "category": "economy"},
            {"text": "油价", "weight": 88, "category": "economy"},
            {"text": "外溢", "weight": 82, "category": "conflict"},
            {"text": "停火斡旋", "weight": 70, "category": "diplomacy"},
        ],
        media_opinions={
            "domestic": [
                {
                    "source": "央视新闻",
                    "view": "维护国际航道畅通符合各方共同利益。",
                    "sentiment_score": 72,
                    "sentiment_label": "中性",
                    "url": "https://example.com/news/g1",
                    "published_at": "2026-08-08T10:00:00+08:00",
                }
            ],
            "international": [
                {
                    "source": "Reuters",
                    "view": "市场关注冲突外溢对霍尔木兹海峡运输的潜在冲击。",
                    "sentiment_score": 78,
                    "sentiment_label": "担忧",
                    "url": "https://example.com/news/g2",
                    "published_at": "2026-08-08T09:40:00+08:00",
                }
            ],
        },
        updated_at="2026-08-08T12:30:00+08:00",
    ),
    "20260811": _impact_block(
        20260811,
        impact_scope="南海周边",
        affected_population="主要涉及海上作业与航行安全关注群体",
        casualties="暂无权威伤亡数据",
        economic_loss="渔业与海上作业短期受扰动，量化数据不足",
        resolution_result="外交沟通持续，局面仍需观察",
        avg_goldstein=-4.6,
        avg_tone=-2.9,
        word_cloud=[
            {"text": "黄岩岛", "weight": 96, "category": "conflict"},
            {"text": "南海", "weight": 90, "category": "conflict"},
            {"text": "舰艇摩擦", "weight": 86, "category": "conflict"},
            {"text": "外交交涉", "weight": 78, "category": "diplomacy"},
            {"text": "航行安全", "weight": 72, "category": "society"},
        ],
        media_opinions={
            "domestic": [
                {
                    "source": "新华社",
                    "view": "中方依法维权并管控海上局势，反对滋事升级。",
                    "sentiment_score": 80,
                    "sentiment_label": "批判",
                    "url": "https://example.com/news/s1",
                    "published_at": "2026-08-11T14:00:00+08:00",
                }
            ],
            "international": [
                {
                    "source": "AP",
                    "view": "中菲海上摩擦再成焦点，地区国家呼吁避免意外冲突。",
                    "sentiment_score": 75,
                    "sentiment_label": "担忧",
                    "url": "https://example.com/news/s2",
                    "published_at": "2026-08-11T13:10:00+08:00",
                }
            ],
        },
        updated_at="2026-08-11T16:00:00+08:00",
    ),
    "20260815": _impact_block(
        20260815,
        impact_scope="东亚舆论场",
        affected_population="以纪念活动参与者与媒体受众为主",
        casualties="暂无权威伤亡数据（纪念节点）",
        economic_loss="暂无直接经济损失量化",
        resolution_result="纪念活动如期举行，相关讨论持续",
        avg_goldstein=-1.6,
        avg_tone=-1.2,
        word_cloud=[
            {"text": "终战日", "weight": 94, "category": "society"},
            {"text": "历史叙事", "weight": 88, "category": "society"},
            {"text": "和平", "weight": 82, "category": "diplomacy"},
            {"text": "东亚安全", "weight": 74, "category": "conflict"},
        ],
        media_opinions={
            "domestic": [
                {
                    "source": "人民日报",
                    "view": "铭记抗战胜利历史，警惕错误历史观回潮。",
                    "sentiment_score": 82,
                    "sentiment_label": "批判",
                    "url": "https://example.com/news/j1",
                    "published_at": "2026-08-15T09:30:00+08:00",
                }
            ],
            "international": [
                {
                    "source": "Kyodo",
                    "view": "日本多地举行终战纪念仪式。",
                    "sentiment_score": 60,
                    "sentiment_label": "中性",
                    "url": "https://example.com/news/j2",
                    "published_at": "2026-08-15T08:50:00+08:00",
                }
            ],
        },
        updated_at="2026-08-15T12:00:00+08:00",
    ),
    "20260818": _impact_block(
        20260818,
        impact_scope="区域经贸",
        affected_population="参会企业与相关产业链从业者",
        casualties="暂无权威伤亡数据",
        economic_loss="暂无负面损失；预期带来议程与合作意向",
        resolution_result="筹备按计划推进",
        avg_goldstein=2.4,
        avg_tone=1.6,
        word_cloud=[
            {"text": "经贸合作", "weight": 90, "category": "economy"},
            {"text": "供应链", "weight": 84, "category": "economy"},
            {"text": "绿色投资", "weight": 76, "category": "economy"},
        ],
        media_opinions={
            "domestic": [
                {
                    "source": "经济日报",
                    "view": "区域合作有助于稳定预期、畅通供应链。",
                    "sentiment_score": 55,
                    "sentiment_label": "正面",
                    "url": "https://example.com/news/e1",
                    "published_at": "2026-08-18T10:20:00+08:00",
                }
            ],
            "international": [
                {
                    "source": "Bloomberg",
                    "view": "论坛筹备引发市场对亚太贸易议程的关注。",
                    "sentiment_score": 50,
                    "sentiment_label": "中性",
                    "url": "https://example.com/news/e2",
                    "published_at": "2026-08-18T09:50:00+08:00",
                }
            ],
        },
        updated_at="2026-08-18T11:00:00+08:00",
    ),
}
