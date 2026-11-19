"""Bounded chapter review and deterministic snapshot/statistics checks."""
import copy
import re
import time
from collections import Counter
from . import checkpoints

REVIEW_VERSION = 3
PRESENTATION_VERSION = 4
PRESENTATION_AUDIT_VERSION = 3  # Distinguish duplicated passages from brief scope reminders.


def repaired_chapter(original, revised):
    """Reject or isolate extra chapters returned by an editor asked for one chapter.

    This selects a whole model-written chapter by its existing heading; it never
    rewrites sentences, citations, or facts. The result still needs a fact audit.
    """
    headings = list(re.finditer(r'^(#{1,6})\s+(.+)$', original, re.M))
    if not headings:
        return revised
    level = min(len(h[1]) for h in headings)
    expected = [h for h in headings if len(h[1])==level]
    target = expected[-1]
    def key(title):
        title = re.sub(r'^(?:第?[一二三四五六七八九十百\d]+[章节、.．:：\s]+)', '', title)
        return re.sub(r'\s+', '', title)
    candidates = list(re.finditer(r'^'+('#'*level)+r'\s+(.+)$', revised, re.M))
    if not candidates:
        return revised  # Citation/content validation still applies to plain prose.
    expected_keys = {key(h[2]) for h in expected}
    if candidates and all(key(h[1]) in expected_keys for h in candidates) and len(candidates)<=len(expected):
        return revised
    matches = [i for i,h in enumerate(candidates) if key(h[1])==key(target[2])]
    if not matches:
        # Models sometimes shorten an existing heading (e.g. 摘要与核心发现 → 摘要).
        matches = [i for i,h in enumerate(candidates) if len(key(h[1]))>=2 and key(target[2]).startswith(key(h[1]))]
    if len(matches)!=1:
        raise RuntimeError('章节修订返回额外章节且目标标题无法唯一定位，未发布')
    index = matches[0]
    block = revised[candidates[index].start():candidates[index+1].start() if index+1<len(candidates) else len(revised)]
    return original[:target.start()] + block.strip()


def sections(markdown, split=True):
    if not split:
        return [markdown]
    # ReportEngine emits H1 for document and chapter titles; other providers use
    # H2 chapters. Preserve those real boundaries instead of joining the end of
    # one chapter to the next title when H2 happens to denote a subsection.
    chapter_level = '#' if len(re.findall(r'^#\s', markdown, re.M)) > 1 else '##'
    blocks = re.split(r'(?=^'+chapter_level+r'\s)', markdown, flags=re.M)
    result = []
    for block in blocks:
        if not block.strip():
            continue
        # Split oversized chapters only at paragraph boundaries.
        current = ''
        for paragraph in block.split('\n\n'):
            if current and len(current)+len(paragraph)>5000:
                result.append(current)
                current = ''
            current += ('\n\n' if current else '')+paragraph
        if current:
            result.append(current)
    # A title alone isn't a chapter and shouldn't require evidence.
    if len(result)>1 and not re.sub(r'^#.*$', '', result[0], flags=re.M).strip():
        title = result.pop(0)
        result[0] = title+'\n\n'+result[0]
    return result


def validate(markdown, snapshot, require_refs=True, check_prose_statistics=True):
    allowed = {e['id'] for e in snapshot['evidence']}
    mentioned = set(re.findall(r'E-[a-f0-9]{12}', markdown))
    if mentioned - allowed:
        raise RuntimeError('报告引用不属于冻结快照，未发布')
    if require_refs and allowed and not mentioned:
        raise RuntimeError('报告未保留证据引用，未发布')
    stats = snapshot.get('sentiment')
    if not stats:
        return
    if sum(stats['counts'].values()) != stats['classified']:
        raise RuntimeError('快照的分类数量与分母不一致，未发布')
    for label, count in stats['counts'].items():
        expected = round(count / stats['classified'] * 100, 1) if stats['classified'] else None
        if stats['percentages'].get(label) != expected:
            raise RuntimeError('快照的情感比例计算不一致，未发布')
        # Check explicit aggregate percentage assertions; sub-platform comparisons
        # are checked semantically using their stated scope.
        for value in re.findall(r'(?:总体样本|全部样本|本次样本|成功分类样本)[^。\n]{0,25}'+re.escape(label)+r'[^。\n\d]{0,12}(\d+(?:\.\d+)?)\s*[%％]', markdown if check_prose_statistics else ''):
            if expected is None or abs(float(value)-expected)>0.11:
                raise RuntimeError('报告样本比例与程序统计不一致，需自动修订')


def review(provider, markdown, snapshot, folder, progress, full=False, presentation=True, writing_context=None):
    chunks = sections(markdown, full)
    approved, audits = [], []
    for index, original in enumerate(chunks):
        path = folder / f'checked-section-{index+1}.json'
        legacy_fingerprint = checkpoints.digest([REVIEW_VERSION, original, snapshot])
        fingerprint = checkpoints.digest([legacy_fingerprint, writing_context]) if writing_context else legacy_fingerprint
        cached = checkpoints.read(path)
        # Legacy approved checks remain valid; the additional chapter context
        # prevents demands to repeat method notes, never changes factual inputs.
        if cached and cached['fingerprint'] in (fingerprint, legacy_fingerprint):
            approved.append(cached['markdown'])
            audits.append(cached['audit'])
            continue
        previous_attempts = list(folder.glob(f'section-{index+1}-*.json'))
        if previous_attempts:
            archive = folder / f'section-{index+1}-previous-{time.time_ns()}'
            archive.mkdir()
            for previous in previous_attempts:
                previous.rename(archive / previous.name)
        current = original
        for attempt in range(3):
            progress(f'审查章节 {index+1}/{len(chunks)} · 自动修订 {attempt}/2')
            checkpoints.write(folder / f'section-{index+1}-draft-{attempt}.json', {'markdown':current})
            issues = []
            try:
                validate(current, snapshot, require_refs=True)
            except RuntimeError as exc:
                issues.append(str(exc))
            if writing_context and hasattr(provider, 'audit_report_contextual'):
                audit = provider.audit_report_contextual(current, snapshot, writing_context)
            else:
                audit = provider.audit_report(current, snapshot)
            if audit.get('approved') is not True:
                issues.extend(x for x in audit.get('issues', []) if isinstance(x, str))
                if not issues:
                    issues.append('章节审查未返回明确通过结果')
            audit = dict(audit, approved=not issues, issues=issues, section=index+1, attempt=attempt)
            checkpoints.write(folder / f'section-{index+1}-audit-{attempt}.json', audit)
            if not issues:
                checkpoints.write(path, dict(fingerprint=fingerprint, markdown=current, audit=audit))
                approved.append(current)
                audits.append(audit)
                break
            if attempt == 2 or not hasattr(provider, 'repair_report'):
                raise RuntimeError('报告核验未通过，未发布：'+'；'.join(issues)[:300])
            if writing_context and hasattr(provider, 'repair_report_contextual'):
                revised = provider.repair_report_contextual(current, snapshot, issues, writing_context)
            else:
                revised = provider.repair_report(current, snapshot, issues)
            current = repaired_chapter(original, revised)
    if presentation and hasattr(provider, 'audit_presentation'):
        # Fact-check batches and editorial chapter IDs are separate contracts.
        # Even short reports can contain several real chapters.
        approved = review_presentation(provider, sections('\n\n'.join(approved)), snapshot, folder, progress)
    result = '\n\n'.join(approved)
    validate(result, snapshot)
    checkpoints.write(folder / 'audit.json', dict(approved=True, chapters=audits))
    return result


def presentation_error(audit, chapter_count):
    if not isinstance(audit, dict) or type(audit.get('approved')) is not bool or not isinstance(audit.get('issues'), list):
        return '全文表达检查返回格式错误'
    for item in audit['issues']:
        if not isinstance(item, dict) or type(item.get('section')) is not int or not 1 <= item['section'] <= chapter_count or not isinstance(item.get('reason'), str) or not item['reason'].strip():
            return f'全文表达检查章节编号或意见无效：section只能使用输入编号1至{chapter_count}，不是正文标题中的章节号'
    if audit['approved'] != (not audit['issues']):
        return '全文表达检查通过状态与意见列表不一致'
    return ''


def review_presentation(provider, chapters, snapshot, folder, progress):
    original = checkpoints.digest([PRESENTATION_VERSION, chapters, snapshot])
    # Resume successful older expression edits, then apply the contextual editor.
    # Only accept caches whose source text and snapshot fingerprints match.
    previous = checkpoints.read(folder / 'presentation-audit-0.json')
    if previous and previous.get('fingerprint')==checkpoints.digest([REVIEW_VERSION, chapters]):
        chapters = list(chapters)
        for attempt in range(2):
            for index in range(len(chapters)):
                old = folder / f'presentation-{attempt}-section-{index+1}'
                repaired = checkpoints.read(old / 'repair.json')
                checked = checkpoints.read(old / 'checked-section-1.json')
                if repaired and checked and checked.get('fingerprint')==checkpoints.digest([REVIEW_VERSION, repaired['markdown'], snapshot]):
                    try:
                        selected = repaired_chapter(chapters[index], checked['markdown'])
                    except RuntimeError:
                        continue  # Invalid legacy edit must not block valid original chapters.
                    if selected==checked['markdown']:
                        chapters[index] = selected
    folder = folder / f'presentation-context-v{PRESENTATION_VERSION}'
    folder.mkdir(exist_ok=True)
    cached = checkpoints.read(folder / 'presentation-complete.json')
    if cached and cached['fingerprint'] == original:
        return cached['chapters']
    chapters = list(chapters)
    review_history = []
    for attempt in range(3):
        if attempt == 2:
            # Clean deterministic renderer artifacts before the final read.
            # This neither adds another prose rewrite nor discards earlier audits.
            from .report_style import remove_redundant_chart_fallbacks
            for index, chapter in enumerate(chapters):
                cleaned = remove_redundant_chart_fallbacks(chapter)
                if cleaned != chapter:
                    progress(f'复核图表格式整理 · 第 {index+1}/{len(chapters)} 章')
                    normalized_folder = folder / f'presentation-normalize-section-{index+1}'
                    normalized_folder.mkdir(exist_ok=True)
                    chapters[index] = review(provider, cleaned, snapshot, normalized_folder, progress,
                        full=False, presentation=False, writing_context=dict(
                            target_section=index+1,
                            editorial_issues=['图表转成的占位表格与紧邻正文包含完全相同的标签和百分比，省略重复表格，保留原文。'],
                            other_chapters=[dict(section=i+1, text=text) for i, text in enumerate(chapters) if i != index]))
        progress(f'全文表达检查 · 修订 {attempt}/2')
        fingerprint = checkpoints.digest([PRESENTATION_VERSION, chapters])
        path = folder / f'presentation-audit-{attempt}.json'
        stored = checkpoints.read(path)
        reuse = stored and stored['fingerprint']==fingerprint
        # Recheck old final rejections once under the corrected editorial policy.
        # Keep completed edits and factual audits; never retry the same policy.
        if reuse and attempt == 2 and stored.get('audit_policy_version') != PRESENTATION_AUDIT_VERSION and stored['audit'].get('approved') is not True:
            checkpoints.write(folder / f'presentation-before-policy-{time.time_ns()}.json', stored)
            reuse = False
        if reuse:
            audit = stored['audit']
        elif review_history and hasattr(provider, 'audit_presentation_followup'):
            if stored and stored['fingerprint'] != fingerprint:
                checkpoints.write(folder / f'presentation-before-format-{time.time_ns()}.json', stored)
            audit = provider.audit_presentation_followup(chapters, review_history)
        else:
            audit = provider.audit_presentation(chapters)
        error = presentation_error(audit, len(chapters))
        if error:
            checkpoints.write(folder / f'presentation-invalid-{attempt}-{time.time_ns()}.json',
                              dict(fingerprint=fingerprint, audit=audit, error=error))
            progress('表达检查返回格式有误，正在重试章节定位')
            if hasattr(provider, 'repair_presentation_audit'):
                audit = provider.repair_presentation_audit(chapters, audit, error)
            else:
                audit = provider.audit_presentation(chapters)
        checkpoints.write(path, dict(fingerprint=fingerprint, audit=audit, review_history_version=1,
                                    audit_policy_version=PRESENTATION_AUDIT_VERSION))
        error = presentation_error(audit, len(chapters))
        if error:
            raise RuntimeError(error + '，未发布；检查结果已保存，可重试')
        issues = audit.get('issues')
        if not isinstance(issues, list):
            raise RuntimeError('全文表达检查返回格式错误，未发布')
        if audit.get('approved') is True and not issues:
            checkpoints.write(folder / 'presentation-complete.json', dict(fingerprint=original, chapters=chapters))
            return chapters
        if attempt == 2 or not issues:
            raise RuntimeError('全文表达检查未通过，草稿已保留')
        grouped = {}
        for item in issues:
            grouped.setdefault(item['section']-1, []).append(item['reason'])
        for index, reasons in grouped.items():
            progress(f'全文表达修订 · 第 {index+1}/{len(chapters)} 章 · 第 {attempt+1}/2 次')
            repair_folder = folder / f'presentation-{attempt}-section-{index+1}'
            repair_folder.mkdir(exist_ok=True)
            key = checkpoints.digest([chapters[index], reasons, snapshot])
            repaired = checkpoints.read(repair_folder / 'repair.json')
            if not repaired or repaired['fingerprint'] != key:
                if hasattr(provider, 'repair_presentation'):
                    text = provider.repair_presentation(chapters[index], snapshot, reasons, chapters, index+1)
                else:
                    text = provider.repair_report(chapters[index], snapshot, reasons)
                repaired = dict(fingerprint=key, markdown=repaired_chapter(chapters[index], text))
                checkpoints.write(repair_folder / 'repair.json', repaired)
            # Expression edits undergo the same factual, citation and statistics checks.
            def recheck_progress(message):
                progress(f'表达修订后复核第 {index+1}/{len(chapters)} 章 · ' + message)
            chapters[index] = review(provider, repaired['markdown'], snapshot, repair_folder, recheck_progress,
                                     full=False, presentation=False, writing_context=dict(
                                         target_section=index+1, editorial_issues=reasons,
                                         other_chapters=[dict(section=i+1, text=text) for i, text in enumerate(chapters) if i != index]))
        review_history.append(dict(attempt=attempt, issues=issues, edits_completed=True))
    raise RuntimeError('全文表达检查未完成')


def statistics_appendix(snapshot):
    stats = snapshot.get('sentiment')
    if not stats:
        return '\n\n## 评论样本统计\n\n没有可用评论样本，未生成情感比例。\n'
    text = '\n\n## 评论样本统计（程序计算）\n\n'
    text += f"采集 {stats['total']} 条；去重 {stats['unique']} 条；抽样 {stats.get('sampled',stats['unique'])} 条；分类成功 {stats['classified']} 条；失败 {stats['failed']} 条。\n\n"
    text += '| 标签 | 数量 | 成功分类样本中的比例 |\n|---|---:|---:|\n'
    for label, count in stats['counts'].items():
        percentage = stats['percentages'].get(label)
        text += f"| {label} | {count} | {str(percentage)+'%' if percentage is not None else '未统计'} |\n"
    return text+'\n以上只描述本次成功分类样本，不代表全网或全部公众。\n'


def collection_appendix(snapshot):
    from .collection import REASONS
    text = '\n\n## 采集范围与停止原因（程序记录）\n\n'
    text += '数量来自当前冻结证据库，帖子按平台内来源编号去重；评论片段不等同于成功分类样本。任务状态仅描述本轮参与调度的查询，不表示整个来源已经穷尽。\n\n'
    text += '| 来源 | 累计留存帖子 | 评论片段 | 网页摘要片段 | 网页正文片段 | 本轮任务状态 |\n|---|---:|---:|---:|---:|---|\n'
    for platform, label in [('wb','微博'),('xhs','小红书'),('dy','抖音'),('web','网页检索')]:
        evidence = [e for e in snapshot['evidence'] if e.get('platform', 'web' if e['source_type'].startswith('web_') else '')==platform]
        counts = Counter(e['source_type'] for e in evidence)
        posts = len({e.get('source_id') or e['id'] for e in evidence if e['source_type']=='social_post'})
        statuses = Counter(c['status'] for c in snapshot.get('collections', []) if c['platform']==platform)
        description = '；'.join(REASONS.get(k, {'complete':'本次请求已完成','partial':'部分完成'}.get(k,k))+' '+str(v)+' 项' for k,v in statuses.items()) or '本轮未调度'
        text += f"| {label} | {posts} | {counts['social_comment']} | {counts['web_summary']} | {counts['web_page']} | {description} |\n"
    return text+'\n完整关键词、分页与请求记录保存在会议采集详情及冻结快照中；网页摘要不冒充已读取的全文。\n'
