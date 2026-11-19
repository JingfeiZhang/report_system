"""Permission failures remain actionable without exposing provider response bodies."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from discussion.providers import model_error_message, report_review_materials


class ProviderErrorTests(unittest.TestCase):
    def test_unpurchased_model_explains_required_external_fix_without_secret(self):
        error = RuntimeError('private upstream text')
        error.status_code = 403
        error.body = {'error': {'code': 'AccessDenied.Unpurchased', 'message': 'private key'}}
        message = model_error_message(error)
        self.assertIn('未开通该模型', message)
        self.assertIn('已完成结果保留', message)
        self.assertNotIn('private', message)

    def test_flat_auth_error_and_transient_error_keep_safe_categories(self):
        error = RuntimeError('private body')
        error.status_code = 401
        error.body = {'code': 'invalid_key'}
        self.assertIn('凭据', model_error_message(error))
        error.status_code = 503
        self.assertIn('HTTP 503', model_error_message(error))
        self.assertNotIn('private', model_error_message(error))

    def test_report_review_has_frozen_classified_comments_and_program_scope(self):
        evidence = [dict(id='E-'+f'{i:012x}', source_id=f'wb:comment:{i}',
                         source_type='social_comment', platform='wb', text='评论'+str(i))
                    for i in range(20)]
        evidence.append(dict(id='E-ffffffffffff', source_type='social_post', platform='wb',
                             source_id='wb:post:1', text='帖子不能冒充评论'))
        snapshot = dict(evidence=evidence, claims=[], sentiment=dict(labels=[
            dict(id=f'wb:comment:{i}', label='正面' if i%2 else '负面') for i in range(19)]))
        a = report_review_materials('', snapshot)
        self.assertEqual(a, report_review_materials('', snapshot))
        self.assertEqual(len(a['classified_comment_examples']),18)
        self.assertNotIn('E-ffffffffffff', [e['id'] for e in a['evidence']])
        self.assertNotIn('E-000000000013', [e['id'] for e in a['evidence']])
        self.assertIn('| 微博 | 1 | 20 |', a['collection_scope'])
        self.assertNotIn('labels', a['sentiment'])
        self.assertEqual(len(snapshot['sentiment']['labels']),19)

    def test_unknown_generated_reference_remains_a_review_finding(self):
        from discussion.evidence import make_evidence
        from discussion.report_checks import validate
        e = make_evidence('真实原文', '原始资料', '段落')
        snapshot = dict(evidence=[e], claims=[dict(text='已核验主张', evidence_ids=[e['id']])])
        text = '生成器错误引用 [E-000000000000]'
        material = report_review_materials(text, snapshot)
        self.assertEqual(material['invalid_reference_ids'], ['E-000000000000'])
        self.assertEqual([x['id'] for x in material['evidence']], [e['id']])
        with self.assertRaises(RuntimeError):
            validate(text, snapshot)


if __name__ == '__main__':
    unittest.main(verbosity=2)
