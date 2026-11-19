"""Shared presentation contract for grounded discussion reports."""
import re
from decimal import Decimal

GENERATION_VERSION = 3

RULES = (
    '报告正文以事件经过、主要发现和争议分析为主，形成连贯分析，不逐条转录智能体发言。'
    '禁止engineQuote、Agent署名及例行的“研究局限与数据缺口声明”警示框。'
    '报告只呈现事件分析，不设置会议与资料范围、采集范围与停止原因、研讨过程与版本记录、用户指导处理记录、'
    '可追溯证据、证据目录或程序计算附录，不输出会议ID、快照ID、工具日志和任务执行情况；这些记录留在工作台。'
    '逐一展开有材料支持的关键节点、各方具体说法、争议焦点、支持与反对理由及其关联，解释事件如何演变。'
    '篇幅优先用于事件细节与分析，不用方法说明、过程复盘、泛泛建议或反复声明凑字数。'
    '事实段落保留[E-编号]引用和准确来源归属；“据某媒体报道”“某评论者认为”可以简洁表达证据层级。'
    '直接影响责任、身份、日期、数字等核心判断的不确定性，就地简洁说明，不藏到附录。'
    '规则优先级：事实边界和就地来源归属高于去重与章节分工。具体指控、争议或推测旁的简短核实状态，'
    '即使结尾待核实事项再次概括，也不是可删除的一般免责声明；不能让读者离开当前段落才知道它尚未证实。'
    '同一一般限制只在指定位置完整说明一次：样本统计首次出现交代样本口径，方法限制集中于评论分析章节，'
    '其他资料缺口集中于结尾的短段待核实事项。其余章节不重复“证据不足”“不代表全网”等免责声明。'
    '摘要可保留一句影响整体理解的限定；不同事实各自需要的来源归属不算重复。'
    '不要为了补篇幅罗列无关的未知问题；证据不足的非必要断言删除，重要问题保留明确边界。'
    '不得通过删除限定将转述、推测或样本结果升级为确定事实或总体结论；免责声明不能抵消无依据断言。'
    '与事件有关的情感比例和观点例证融入舆情分析段落，首次出现时简要写明样本量和范围，不另设“评论样本统计（程序计算）”章节。'
    '前文已交代样本口径时，后文可用“上述样本”“抽样评论”指代，不要求每章重新写全；'
    '为理解某项结论而简短重提样本数量或来源不算实质重复，不能仅因单句中再次出现这些数字就否决全文。'
    '不要把帖子观点当作评论，评论情感分布不能证明具体诉求的频率；无评论时省略比例。'
    'image_analysis证据是视觉模型对原图的提取，引用其编号并写明“截图显示/图片文字提及”等归属；'
    '不能将截图内说法当作已证实的现实事实，不能将同图转载与原帖视为独立来源。'
    '模糊、截断、无法辨认内容不能补写；图片中的身份、日期和指控需结合其他来源核验。'
)


def chapter_prompt(base):
    """Replace conflicting legacy presentation instructions, preserving the IR schema."""
    replacements = {
        '9. ': '9. 按outline分工写作，优先使用段落、必要的列表和表格。',
        '10. ': '10. 本报告不使用engineQuote；将各角色贡献整合进分析，保留来源归属与引用。',
        '17. ': '17. 不为版面丰富而添加提示框、KPI或重复表格。',
    }
    lines = []
    for line in base.splitlines():
        lines.append(next((value for key, value in replacements.items() if line.startswith(key)), line))
    return '\n'.join(lines) + '\n协同研讨报告表达规则：' + RULES


def remove_redundant_chart_fallbacks(markdown):
    """Omit a generated percentage chart table only when adjacent prose has all its data.

    ReportEngine's Markdown chart fallback uses a placeholder series header.
    Require a single series, explicit percentage labels, matching numeric cells,
    and every label/value pair in the preceding paragraph. Ordinary tables,
    unique data, citations and explanatory cells are preserved verbatim.
    """
    blocks = markdown.split('\n\n')
    kept = []
    for block in blocks:
        lines = block.strip().splitlines()
        cells = lambda line: [cell.strip() for cell in line.strip().strip('|').split('|')]
        redundant = False
        if kept and len(lines) >= 4 and all(line.strip().startswith('|') for line in lines):
            rows = [cells(line) for line in lines]
            if (all(len(row) == 2 for row in rows)
                    and rows[0][0] == '类别' and re.fullmatch(r'系列\s*\d+', rows[0][1])
                    and all(re.fullmatch(r':?-{3,}:?', cell) for cell in rows[1])):
                previous = kept[-1]
                redundant = not previous.lstrip().startswith(('#', '|', '```'))
                for label, value in rows[2:]:
                    match = re.fullmatch(r'([^|\[\]()（）]+?)\s*[（(](\d+(?:\.\d+)?)\s*[%％][）)]', label)
                    if not match or not re.fullmatch(r'\d+(?:\.\d+)?', value):
                        redundant = False
                        break
                    name, percentage = match.groups()
                    numbers = re.findall(re.escape(name.strip()) + r'[^。\n\d%％]{0,24}(\d+(?:\.\d+)?)\s*[%％]', previous)
                    if Decimal(value) != Decimal(percentage) or not any(Decimal(n) == Decimal(value) for n in numbers):
                        redundant = False
                        break
        if not redundant:
            kept.append(block)
    return '\n\n'.join(kept)
