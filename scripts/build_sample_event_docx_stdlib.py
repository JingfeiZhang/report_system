# -*- coding: utf-8 -*-
"""仅用标准库生成最小 docx（无 python-docx 依赖）。"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

CONTENT = """\
【说明】本文件为 BettaFish event2 / Report Agent 联调用的虚构测试文档。文中番号、地名、时间、兵力数据均为合成样例，不代表任何真实事件。

文档编号：TEST-EVT-20260312-001
事件标签：联合演习 / 兵力机动 / 港口集结
编制单位：东港训练区导调中心（虚构）
签发时间：2026年3月15日 09:30

一、事件概况
2026年3月12日至3月18日，「蓝盾-2026」陆海联合演习进入第二阶段的兵力机动与前沿部署环节。蓝方特遣队辖第17合成旅、第52支援营及配属工兵连，自东港训练区北营区出发，沿滨海公路向青岭镇方向实施昼间机动，计划在青岭港外围建立临时集结点，并同步展开装卸与补给演练。导调组记录显示，截至3月15日08:00，主力梯队已完成约62% 的预定机动里程，未发现影响演习进度的重大装备故障。

二、时间线（摘要）
• 3月12日 06:00 机动启动：第17合成旅从东港训练区北营区出发，先遣侦察分队前出至青岭镇东侧观察点。
• 3月13日 14:30 途中集结：滨海公路 K128 临时停车区完成第一次整队，工兵连展开道路保障演练。
• 3月14日 20:00 夜间转场：蓝方特遣队第二梯队完成夜间隐蔽转场，通信干扰条件下保持连级联络。
• 3月15日 08:00 前沿部署：先遣营在青岭港外围3公里处建立临时集结点，开始装卸与补给科目。

三、涉及兵力与部署
第17合成旅（合成旅）— 青岭镇至青岭港机动途中，演习主力。
第52支援营（支援营）— 东港训练区南补给点，负责油料与弹药前送。
蓝方工兵连 — 滨海公路 K128 段，道路抢修与障碍设置演练。
先遣侦察分队 — 青岭镇东侧观察点，提供路线与港口周边态势。

四、当前态势研判
1. 机动节奏整体符合预案，3月14日夜间转场较计划延长约40分钟。
2. 通信保障：干扰条件下连级联络正常，营旅级卫星回传偶发延迟。
3. 后勤：青岭港外围临时集结点装卸位不足，3月15日可能出现排队。
4. 天气：15日午后可能有中雨，滨海公路部分路段需降速。

五、待核实项
• 第17合成旅3月15日实到兵力与编制表是否一致。
• 青岭港外围临时集结点实际可容纳车辆数。
• 3月14日夜间转场延长的具体原因（道路、指挥或装备）。"""


def _w_p(text: str) -> str:
    return (
        "<w:p><w:r><w:rPr><w:rFonts w:ascii=\"宋体\" w:hAnsi=\"宋体\" "
        "w:eastAsia=\"宋体\"/><w:sz w:val=\"21\"/></w:rPr>"
        f"<w:t xml:space=\"preserve\">{escape(text)}</w:t></w:r></w:p>"
    )


def _document_xml(paragraphs: list[str]) -> str:
    body = "".join(_w_p(p) for p in paragraphs)
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    )


def write_docx(path: Path, *, title: str, paragraphs: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    doc_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""

    all_paras = [title, ""] + paragraphs
    doc_xml = _document_xml(all_paras)

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        zf.writestr("word/document.xml", doc_xml.encode("utf-8"))


def main() -> Path:
    root = Path(__file__).resolve().parent.parent
    out = root / "tests" / "fixtures" / "蓝盾2026联合演习兵力机动事件概述_测试.docx"
    paras = [p for p in CONTENT.split("\n\n") if p.strip()]
    write_docx(
        out,
        title="蓝盾-2026 联合演习兵力机动事件概述（测试附件）",
        paragraphs=paras,
    )
    print(f"已生成: {out}")
    return out


if __name__ == "__main__":
    main()
