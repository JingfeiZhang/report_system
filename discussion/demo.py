"""Explicitly fictional replay, without network or model calls."""
from .evidence import make_evidence

TITLE = "星舟平台服务故障与用户投诉（虚构案例）"
FOLLOWUP = "请核查负面评论比例的样本范围，并补充平台回应与恢复时间线。"


def evidence():
    items = [
        ("平台故障记录（虚构）", "2026-10-12 09:10 登录接口异常；09:25 发布故障公告；10:05 核心服务恢复；10:40 完成数据核对。"),
        ("媒体观察简报（虚构）", "甲媒体关注登录失败对用户工作的影响；乙媒体关注平台公告是否及时。两篇报道均没有独立统计受影响用户人数。"),
        ("用户样本说明（虚构）", "本案例有10条人为编写、互不重复的演示评论，其中6条负面、2条中性、2条正面。样本仅用于展示，不代表真实用户或全体公众。"),
        ("平台后续回应（虚构）", "平台于2026-10-12 10:20回应：登录异常源于配置变更，已回滚；对受影响用户提供工单处理。未提供独立核验的受影响人数。")]
    return [make_evidence(t, title, f"演示资料 {i + 1}", "demo", date="2026-10-12")
            for i, (title, t) in enumerate(items)]


def card(role, task, pool, round_number):
    refs = [e["id"] for e in pool]
    later = round_number > 1
    conclusions = {
        "facts": "09:10出现登录异常，10:05核心服务恢复。" + ("10:20平台说明已回滚配置；10:40完成数据核对。" if later else "故障与后续数据核对的完成时间需要区分。"),
        "media": "两类报道分别关注用户使用受阻和平台回应及时性；报道差异不等于公众态度分布。",
        "sentiment": "演示样本共10条，6条负面，占60%。这一比例仅描述虚构样本。" if later else "已找到10条演示评论的样本说明。正式写入比例前，应同时说明样本来源与范围。",
        "review": "应区分核心服务恢复与数据核对完成；不得把10条虚构样本的情感分布概括为公众情绪。"}
    chosen = {"facts": [refs[0], refs[3]], "media": [refs[1]], "sentiment": [refs[2]], "review": refs}
    return {"conclusion": conclusions[role], "evidence_ids": chosen[role],
            "limitations": ["所有材料均为虚构演示资料，不代表真实事件。", "受影响总人数没有独立证据。"],
            "response": "已按用户追问区分样本范围、平台回应和恢复时间。" if later else "等待核验角色交叉检查。",
            "changes": "补充平台10:20回应；明确60%仅对应演示样本。" if later else "首轮建立事件事实与待核验清单。"}


def summary(round_number):
    return {"overview": "各角色已完成本轮研讨。" + ("已根据预设追问补充回应和样本说明。" if round_number > 1 else "可以生成初版报告，或继续核查样本与平台回应。"),
            "agreements": ["核心服务恢复不等于全部数据核对完成。", "报道观点与评论样本需要分开陈述。"],
            "disagreements": ["尚无独立资料确认受影响用户总数。"],
            "gaps": ["真实用户样本与影响范围仍待补充。"],
            "next_questions": [FOLLOWUP] if round_number == 1 else ["如需分析真实事件，请创建真实分析会议。"]}


def debate_card(role, task, pool, round_number, context):
    """Scripted example uses the same orchestration, IDs and barriers as live calls."""
    result = card(role, task['question'], pool, round_number)
    phase = task.get('phase')
    viewpoints = {
        'media': {
            'proposals': ['甲媒体强调使用受阻，乙媒体强调公告及时性，呈现出两种报道关注点。',
                          '报道关注点不同，但两篇都没有独立统计受影响人数；不能据此推导媒体或用户的整体态度。'],
            'agreement': '两篇虚构报道的关注点不同。',
            'question': '报道关注点不同，是否足以说明媒体立场不同？',
            'reason': '现有资料只描述报道主题，没有足够内容判断立场。',
            'resolution': '将立场判断改为有出处的关注点比较，说明是否有独立调查。',
            'query': '星舟平台 媒体报道 独立调查',
            'revision': '甲媒体关注用户使用受阻，乙媒体关注公告及时性。材料支持报道关注点的差异，尚不能据此判定两家媒体立场对立；两篇均未独立统计受影响人数。',
            'change': '将“立场差异”收窄为“报道关注点差异”，补充独立调查范围。',
            'reply': '维持关注点比较；不把主题差异表述为立场对立，也不据此估计公众态度。',
        },
        'sentiment': {
            'proposals': ['虚构资料说明10条演示评论中有6条负面，负面表达在这个样本中较多。',
                          '同一资料的6/10对应60%；样本由人工编写，不能据此估计真实用户的负面比例或平台差异。'],
            'agreement': '同一份演示样本说明给出6/10，计算比例为60%。',
            'question': '这个比例能否支持“真实用户大多不满”的判断？',
            'reason': '样本为人工编写，没有真实用户抽样和跨平台来源。',
            'resolution': '保留程序一致的6/10口径，把情绪解释限定为演示样本。',
            'query': '星舟平台 评论 样本来源',
            'revision': '10条人工编写的虚构评论中，负面6条、中性2条、正面2条，对应60%、20%、20%。负面表达较多仅是这份演示样本的特征，不能用于判断真实用户或比较平台。',
            'change': '统计值保持一致，将解释限定为人工编写的演示样本。',
            'reply': '维持6/10的统计结果，撤去对真实用户的推断，不作平台间比较。',
        },
    }
    scenario = viewpoints.get(role)
    if phase == 'proposal' and scenario:
        result['conclusion'] = scenario['proposals'][1 if task['agent_id'].endswith('-2') else 0]
    if phase == 'proposal' and role == 'facts':
        result['conclusion'] = ('登录异常于09:10出现，10:05核心服务恢复，可据此描述服务恢复节点。'
                                if task['agent_id'].endswith('-1') else
                                '记录同时列出10:05服务恢复和10:40数据核对完成；不能用单一时点代表全部处置结束。')
    if phase == 'peer_review':
        target = context['target']
        first = task['exchange'] == 1
        challenge = dict(claim_id=target['claims'][0]['id'], question='如何区分服务恢复、平台回应和全部处置结束？',
                         reason='三个时间点含义不同，概括为故障结束可能扩大原始记录。',
                         resolution='分别列出09:10、10:05、10:20、10:40的含义，并保留平台回应归属。',
                         search_query='星舟平台 恢复时间 后续回应')
        result.update(conclusion='认可故障记录中的时间节点；需要进一步区分恢复范围与回应来源。' if first else '修订已区分不同节点，目前没有新增实质质疑。',
                      review=dict(agreements=['同意材料记载的服务恢复时间'], challenges=[challenge] if first else [],
                                  own_revision='我的表述也应区分平台自述与独立核查。'))
        if scenario:
            challenge.update(question=scenario['question'], reason=scenario['reason'], resolution=scenario['resolution'], search_query=scenario['query'])
            result.update(conclusion=scenario['agreement'], review=dict(agreements=[scenario['agreement']],
                challenges=[challenge] if first else [], own_revision=scenario['change']))
    if phase == 'revision':
        result.update(conclusion='09:10登录异常；10:05核心服务恢复；10:20平台自述已回滚配置；10:40完成数据核对。不能据此认定全部用户影响已经消除。',
                      changes='限定“恢复”的范围，补充平台回应归属；样本比例仅描述演示样本。',
                      response='已逐项回应恢复时间和来源归属质疑，受影响人数仍缺独立证据。',
                      replies=[dict(challenge_id=c['id'], action='qualify', response='已分别说明各时间节点；平台原因说明保持归属。', evidence_ids=result['evidence_ids'])
                               for r in context['peer_reviews'] for c in r['challenges'] if c['target_agent'] == task['agent_id']])
        if scenario:
            result.update(conclusion=scenario['revision'], changes=scenario['change'], response=scenario['reply'])
            for reply in result['replies']:
                reply['response'] = scenario['reply']
    if phase == 'verification':
        result['verifications'] = [dict(claim_id=c['id'], status='supported', reason='仅在虚构案例材料范围内得到支持，非真实事件核实。', evidence_ids=c['evidence_ids'])
                                   for p in context['candidates'] for c in p['claims']]
        result['replies'] = [dict(challenge_id=c['id'], resolved=c['status'] == 'responded', reason='虚构演示的修订稿已回应质疑并限定表述范围。', evidence_ids=result['evidence_ids'])
                            for r in context['peer_reviews'] for c in r['challenges']]
    old = context.get('own', {}).get('claims', []) if isinstance(context, dict) else []
    result['claims'] = [dict(text=result['conclusion'], kind='fact' if role == 'facts' else 'inference',
                             scope='虚构演示资料', evidence_ids=result['evidence_ids'], opposing_evidence_ids=[],
                             previous_id=old[0]['id'] if old else None, action='qualify' if phase == 'revision' else 'maintain')]
    return result


def report(meeting, snapshot):
    lines = [f"# {meeting['title']}\n", "**演示模式：以下内容由预设脚本生成，全部材料均为虚构。**\n",
             "## 摘要\n", snapshot["summary"]["overview"], "\n## 事件与分析\n"]
    for item in snapshot["cards"]:
        lines += [f"### {item['role_name']}", item["conclusion"] + " " + " ".join(f"[{ref}]" for ref in item["evidence_ids"]),
                  "\n变化：" + item["changes"]]
    lines += ["\n## 分歧与局限\n", "\n".join(snapshot["summary"]["disagreements"] + snapshot["summary"]["gaps"])]
    return "\n\n".join(lines)

