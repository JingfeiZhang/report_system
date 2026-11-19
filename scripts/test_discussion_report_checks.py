import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from discussion.report_checks import sections, validate, review, statistics_appendix, collection_appendix
from discussion.evidence import make_evidence


class ReportChecksTests(unittest.TestCase):
    def setUp(self):
        self.e = make_evidence('原始记录', '来源', '段落1')
        self.snapshot = dict(evidence=[self.e], sentiment=None)
        self.md = '# 标题\n\n## 事实\n\n记录 ['+self.e['id']+']\n\n## 局限\n\n此来源未提供更多资料 ['+self.e['id']+']。'

    def test_title_and_all_chapters_retained(self):
        parts = sections(self.md)
        self.assertEqual(len(parts), 2)
        self.assertIn('标题', parts[0])
        self.assertIn('事实', parts[0])
        self.assertIn('局限', parts[1])

    def test_engine_chapter_titles_are_not_mixed_with_previous_subsection(self):
        md = '# 文档\n\n# 第一章\n\n## 子节甲\n\n内容甲\n\n# 第二章\n\n## 子节乙\n\n内容乙'
        parts = sections(md)
        self.assertEqual(len(parts), 2)
        self.assertIn('文档', parts[0])
        self.assertIn('内容甲', parts[0])
        self.assertNotIn('第二章', parts[0])
        self.assertTrue(parts[1].startswith('# 第二章'))

    def test_unknown_citation_rejected(self):
        with self.assertRaises(RuntimeError):
            validate(self.md+' [E-000000000000]', self.snapshot)

    def test_bad_denominator_rejected(self):
        self.snapshot['sentiment'] = dict(counts={'负面':3},classified=4,percentages={'负面':75})
        with self.assertRaises(RuntimeError):
            validate(self.md,self.snapshot)

    def test_no_samples_no_percentage(self):
        self.assertNotIn('%', statistics_appendix(self.snapshot))

    def test_collection_counts_come_from_snapshot_not_resumed_task_totals(self):
        post = dict(self.e, platform='wb', source_type='social_post', source_id='wb:post:1')
        self.snapshot['evidence'] = [post, dict(post, id='E-111111111111')]
        self.snapshot['collections'] = [dict(platform='wb', status='budget_exhausted', post_count=50)]
        text = collection_appendix(self.snapshot)
        self.assertIn('| 微博 | 1 | 0 | 0 | 0 |', text)
        self.assertIn('时间预算用完', text)
        self.assertNotIn('50', text)

    def test_historical_prose_keeps_own_denominator_but_snapshot_math_is_checked(self):
        self.snapshot['sentiment'] = dict(counts={'负面':3,'中性':1}, classified=4, percentages={'负面':75,'中性':25})
        historic = self.md+'\n第一轮原始回复：本次样本负面占50%。'
        with self.assertRaises(RuntimeError):
            validate(historic, self.snapshot)
        validate(historic, self.snapshot, check_prose_statistics=False)
        self.snapshot['sentiment']['classified'] = 5
        with self.assertRaises(RuntimeError):
            validate(historic, self.snapshot, check_prose_statistics=False)

    def test_chapter_repair_and_successful_audit_reused(self):
        class Provider:
            calls = 0
            repairs = 0
            def audit_report(self, text, snapshot):
                self.calls += 1
                return dict(approved='错误' not in text,issues=['删除错误'] if '错误' in text else [])
            def repair_report(self,text,snapshot,issues):
                self.repairs += 1
                return text.replace('错误','限定')
        provider = Provider()
        with tempfile.TemporaryDirectory() as directory:
            result=review(provider,self.md+'错误',self.snapshot,Path(directory),lambda _:None,True)
            self.assertNotIn('错误',result)
            self.assertEqual(provider.repairs,1)
            calls=provider.calls
            self.assertEqual(review(provider,self.md+'错误',self.snapshot,Path(directory),lambda _:None,True),result)
            self.assertEqual(provider.calls,calls)

    def test_audit_failure_does_not_publish(self):
        class Provider:
            def audit_report(self,*args): return dict(approved=False,issues=['依据不足'])
            def repair_report(self,text,*args): return text
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                review(Provider(),self.md,self.snapshot,Path(directory),lambda _:None,True)
            self.assertFalse((Path(directory)/'audit.json').exists())

    def test_model_approval_cannot_publish_chapter_without_citations(self):
        class Provider:
            def audit_report(self,*args): return dict(approved=True,issues=[])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError,'未保留证据引用'):
                review(Provider(),'# 具体事件事实\n\n没有引用。',self.snapshot,Path(directory),lambda _:None,True)


    def test_presentation_changes_only_target_and_rechecks_facts_with_resume(self):
        class Provider:
            checked = []
            repairs = 0
            def audit_report(self, text, snapshot):
                self.checked.append(text)
                return dict(approved=True,issues=[])
            def audit_presentation(self, chapters):
                return dict(approved='重复声明' not in chapters[-1],
                            issues=[dict(section=2,reason='删去重复声明，第一章已交代')] if '重复声明' in chapters[-1] else [])
            def repair_report(self, text, snapshot, issues):
                self.repairs += 1
                return text.replace('重复声明','')
        provider=Provider()
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            result=review(provider,self.md+'重复声明',self.snapshot,folder,lambda _:None,True)
            self.assertNotIn('重复声明',result)
            self.assertEqual(len(provider.checked),3)
            self.assertEqual(provider.repairs,1)
            self.assertEqual(result,review(provider,self.md+'重复声明',self.snapshot,folder,lambda _:None,True))
            self.assertEqual(len(provider.checked),3)

    def test_presentation_cannot_remove_citations_or_rewrite_unknown_chapter(self):
        class Provider:
            section = 9
            def audit_report(self,*args): return dict(approved=True,issues=[])
            def audit_presentation(self, chapters): return dict(approved=False,issues=[dict(section=self.section,reason='缩短')])
            def repair_report(self,*args): return '无引用'
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);provider=Provider()
            with self.assertRaisesRegex(RuntimeError,'章节编号'):
                review(provider,self.md,self.snapshot,folder,lambda _:None,True)
            self.assertFalse((folder/'audit.json').exists())
        provider.section=1
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError,'未保留证据引用'):
                review(provider,self.md,self.snapshot,Path(directory),lambda _:None,True)

    def test_short_report_presentation_uses_chapters_not_single_fact_check_block(self):
        class Provider:
            checked = []
            def audit_report(self, text, snapshot):
                self.checked.append(text)
                return dict(approved=True, issues=[])
            def audit_presentation(self, chapters):
                self.chapter_counts = len(chapters)
                return dict(approved='重复声明' not in '\n'.join(chapters),
                            issues=[dict(section=2, reason='第二章重复声明，第一章已交代')]
                            if '重复声明' in '\n'.join(chapters) else [])
            def repair_report(self, text, snapshot, issues):
                return text.replace('重复声明', '')
        provider = Provider()
        with tempfile.TemporaryDirectory() as directory:
            result = review(provider, self.md+'重复声明', self.snapshot, Path(directory), lambda _:None, full=False)
            self.assertEqual(provider.chapter_counts, 2)
            self.assertNotIn('重复声明', result)
            self.assertEqual(len(provider.checked), 2)  # whole report, then only edited chapter
            self.assertEqual(result, review(provider, self.md+'重复声明', self.snapshot, Path(directory), lambda _:None, full=False))
            self.assertEqual(len(provider.checked), 2)

    def test_chapter_editor_cannot_replace_one_chapter_with_whole_report(self):
        from discussion.report_checks import repaired_chapter
        original = '# 报告标题\n\n# 摘要\n原摘要'
        revised = '# 报告标题\n\n# 摘要\n模型修订摘要 [E-123456789012]\n\n# 二、事件经过\n其他章节'
        result = repaired_chapter(original, revised)
        self.assertIn('模型修订摘要', result)
        self.assertNotIn('其他章节', result)
        self.assertIn('# 报告标题', result)
        with self.assertRaisesRegex(RuntimeError,'目标标题'):
            repaired_chapter(original,'# 错误章节\n内容\n\n# 另一章\n内容')

    def test_invalid_presentation_id_is_repaired_without_rewriting_report(self):
        class Provider:
            feedback = None
            def audit_report(self, *args): return dict(approved=True, issues=[])
            def audit_presentation(self, chapters):
                return dict(approved=False, issues=[dict(section=9, reason='错误编号')])
            def repair_presentation_audit(self, chapters, audit, error):
                self.feedback = (audit, error)
                return dict(approved=True, issues=[])
        provider = Provider()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            result = review(provider, self.md, self.snapshot, folder, lambda _:None)
            self.assertEqual(result, '\n\n'.join(sections(self.md)))
            self.assertIn('编号', provider.feedback[1])
            self.assertEqual(len(list(folder.rglob('presentation-invalid-*.json'))), 1)

    def test_invalid_presentation_cache_does_not_permanently_block_resume(self):
        class Provider:
            calls = 0
            valid = False
            def audit_report(self, *args): return dict(approved=True, issues=[])
            def audit_presentation(self, chapters):
                self.calls += 1
                return dict(approved=self.valid, issues=[] if self.valid else [dict(section=99, reason='错误编号')])
        provider = Provider()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with self.assertRaisesRegex(RuntimeError, '章节编号'):
                review(provider, self.md, self.snapshot, folder, lambda _:None)
            self.assertEqual(provider.calls, 2)
            self.assertFalse((folder/'audit.json').exists())
            provider.valid = True
            review(provider, self.md, self.snapshot, folder, lambda _:None)
            self.assertEqual(provider.calls, 3)
            self.assertTrue((folder/'audit.json').exists())

    def test_presentation_followup_keeps_prior_editorial_decisions(self):
        class Provider:
            history = None
            def audit_report(self, *args): return dict(approved=True, issues=[])
            def audit_presentation(self, chapters):
                return dict(approved=False, issues=[dict(section=2, reason='第一章保留说明，第二章删除重复声明')])
            def repair_report(self, text, *args): return text.replace('重复声明', '')
            def audit_presentation_followup(self, chapters, history):
                self.history = history
                return dict(approved=True, issues=[])
        provider = Provider()
        with tempfile.TemporaryDirectory() as directory:
            review(provider, self.md+'重复声明', self.snapshot, Path(directory), lambda _:None)
            self.assertEqual(provider.history[0]['issues'][0]['section'], 2)
            self.assertTrue(provider.history[0]['edits_completed'])
            self.assertIn('第一章保留', provider.history[0]['issues'][0]['reason'])

    def test_legacy_final_rejection_rechecked_with_history_without_repeating_edits(self):
        class Provider:
            repairs = 0
            legacy = True
            followups = 0
            def audit_report(self, *args): return dict(approved=True, issues=[])
            def audit_presentation(self, chapters):
                return dict(approved=False, issues=[dict(section=2, reason='保持第一章说明，删除第二章重复段落')])
            def repair_report(self, text, *args):
                self.repairs += 1
                return text+'\n调整'+str(self.repairs)
            def audit_presentation_followup(self, chapters, history):
                self.followups += 1
                return self.audit_presentation(chapters) if self.legacy else dict(approved=True, issues=[])
        provider = Provider()
        with tempfile.TemporaryDirectory() as directory:
            from discussion import checkpoints
            folder = Path(directory)
            with self.assertRaisesRegex(RuntimeError, '草稿已保留'):
                review(provider, self.md, self.snapshot, folder, lambda _:None)
            path = folder/'presentation-context-v4/presentation-audit-2.json'
            stored = checkpoints.read(path)
            stored.pop('review_history_version')  # Emulate the old last audit without history.
            stored.pop('audit_policy_version')
            checkpoints.write(path, stored)
            provider.legacy = False
            calls = provider.followups
            result = review(provider, self.md, self.snapshot, folder, lambda _:None)
            self.assertEqual(provider.repairs, 2)
            self.assertEqual(provider.followups, calls+1)
            self.assertIn('调整2', result)

    def test_policy_upgrade_rechecks_rejection_once_without_repeating_repairs(self):
        class Provider:
            audits = 0
            repairs = 0
            def audit_report(self, *args): return dict(approved=True, issues=[])
            def audit_presentation(self, chapters):
                self.audits += 1
                return dict(approved=False, issues=[dict(section=2, reason='尚有实质重复段落')])
            def audit_presentation_followup(self, chapters, history):
                return self.audit_presentation(chapters)
            def repair_report(self, text, *args):
                self.repairs += 1
                return text+'\n模型修订'+str(self.repairs)
        from discussion import checkpoints
        provider = Provider()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with self.assertRaisesRegex(RuntimeError, '草稿已保留'):
                review(provider, self.md, self.snapshot, folder, lambda _:None)
            path = folder/'presentation-context-v4/presentation-audit-2.json'
            stored = checkpoints.read(path)
            stored['audit_policy_version'] = 1
            checkpoints.write(path, stored)
            audits = provider.audits
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, '草稿已保留'):
                    review(provider, self.md, self.snapshot, folder, lambda _:None)
                self.assertEqual(provider.audits, audits+1)
                self.assertEqual(provider.repairs, 2)
                self.assertFalse((folder/'audit.json').exists())

    def test_editorial_fact_checks_and_repairs_share_chapter_context(self):
        class Provider:
            seen = []
            def audit_report(self, *args): return dict(approved=True, issues=[])
            def audit_presentation(self, chapters):
                return dict(approved='重复说明' not in chapters[-1],
                            issues=[dict(section=2, reason='口径已在第一章说明，删除重复说明')]
                            if '重复说明' in chapters[-1] else [])
            def repair_report(self, text, *args):
                return text.replace('重复说明', '未限定的断言')
            def audit_report_contextual(self, text, snapshot, context):
                self.seen.append(('audit', context))
                return dict(approved='未限定的断言' not in text,
                            issues=['限定断言，保留既定章节分工'] if '未限定的断言' in text else [])
            def repair_report_contextual(self, text, snapshot, issues, context):
                self.seen.append(('repair', context))
                return text.replace('未限定的断言', '有归属的观点')
        provider = Provider()
        with tempfile.TemporaryDirectory() as directory:
            result = review(provider, self.md+'重复说明', self.snapshot, Path(directory), lambda _:None)
            self.assertIn('有归属的观点', result)
            self.assertNotIn('重复说明', result)
            self.assertEqual([kind for kind, _ in provider.seen], ['audit', 'repair', 'audit'])
            context = provider.seen[0][1]
            self.assertEqual(context['target_section'], 2)
            self.assertIn('口径已在第一章', context['editorial_issues'][0])
            self.assertIn('记录', context['other_chapters'][0]['text'])
            self.assertTrue(all(value == context for _, value in provider.seen))
            review(provider, self.md+'重复说明', self.snapshot, Path(directory), lambda _:None)
            self.assertEqual(len(provider.seen), 3)

    def test_chart_fallback_cleanup_keeps_unique_values_and_ordinary_tables(self):
        from discussion.report_style import remove_redundant_chart_fallbacks as clean
        prose = '样本中负面情感占60.0%，中性占40%。'
        table = '| 类别 | 系列1 |\n| --- | --- |\n| 负面 (60%) | 60 |\n| 中性 (40%) | 40 |'
        self.assertEqual(clean(prose+'\n\n'+table), prose)
        for changed in (table.replace('40 |', '41 |'), table.replace('中性 (40%)', '正面 (40%)'),
                        table.replace('系列1', '情感比例'), table+'\n| 其他 (5%) | 5 |',
                        table.replace('60 |', '60 [E-123456789012] |')):
            self.assertEqual(clean(prose+'\n\n'+changed), prose+'\n\n'+changed)
        self.assertEqual(clean('样本中负面占60条，中性占40条。\n\n'+table),
                         '样本中负面占60条，中性占40条。\n\n'+table)

    def test_final_chart_cleanup_is_fact_checked_and_resumable(self):
        class Provider:
            repairs = 0
            checked = []
            def audit_report(self, text, snapshot):
                self.checked.append(text)
                return dict(approved=True, issues=[])
            def audit_presentation(self, chapters):
                return dict(approved=False, issues=[dict(section=2, reason='删除重复分析段落')])
            def audit_presentation_followup(self, chapters, history):
                if len(history) == 2:
                    return dict(approved='系列1' not in chapters[-1], issues=[])
                return self.audit_presentation(chapters)
            def repair_report(self, text, *args):
                self.repairs += 1
                return text+'\n已修订'+str(self.repairs)
        provider = Provider()
        md = self.md+'\n\n样本负面占60%，中性占40%。\n\n| 类别 | 系列1 |\n| --- | --- |\n| 负面 (60%) | 60 |\n| 中性 (40%) | 40 |\n\n正文结束。'
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            result = review(provider, md, self.snapshot, folder, lambda _:None)
            self.assertNotIn('系列1', result)
            self.assertIn('负面占60%', result)
            self.assertEqual(provider.repairs, 2)
            self.assertNotIn('系列1', provider.checked[-1])
            calls = len(provider.checked)
            self.assertEqual(review(provider, md, self.snapshot, folder, lambda _:None), result)
            self.assertEqual(len(provider.checked), calls)
            self.assertEqual(provider.repairs, 2)


if __name__=='__main__': unittest.main(verbosity=2)
