"""Report interruption must not repeat successful paid chapter calls."""
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from discussion.report_cache import install


@dataclass
class Section:
    chapter_id: str = 'chapter-1'
    slug: str = 'chapter-1'
    title: str = '章节'
    order: int = 1


class CacheTests(unittest.TestCase):
    def test_resume_reuses_success_only_and_invalidates_changed_input(self):
        calls = []
        saved = []
        failed = [True]
        def generate(section, context, *args, **kwargs):
            calls.append(section.chapter_id)
            if section.chapter_id == 'chapter-2' and failed[0]:
                failed[0] = False
                raise TimeoutError()
            return dict(chapterId=section.chapter_id, blocks=[context])
        def agent():
            return SimpleNamespace(document_layout_node=SimpleNamespace(run=lambda *args: dict(
                title='标题',hero={},tocPlan=[],tocTitle='目录')),
                chapter_generation_node=SimpleNamespace(run=generate),
                chapter_storage=SimpleNamespace(persist_chapter=lambda *args: saved.append(args)))
        with tempfile.TemporaryDirectory() as directory:
            first = agent()
            install(first,directory,['model'],lambda _: None)
            first.chapter_generation_node.run(Section(),{'evidence':'v1'},directory)
            with self.assertRaises(TimeoutError):
                first.chapter_generation_node.run(Section('chapter-2'),{},directory)
            resumed = agent()
            install(resumed,directory,['model'],lambda _: None)
            resumed.chapter_generation_node.run(Section(),{'evidence':'v1'},directory)
            resumed.chapter_generation_node.run(Section('chapter-2'),{},directory)
            self.assertEqual(calls,['chapter-1','chapter-2','chapter-2'])
            resumed.chapter_generation_node.run(Section(),{'evidence':'v2'},directory)
            self.assertEqual(calls[-1],'chapter-1')
            self.assertEqual(len(saved),4)


if __name__ == '__main__':
    unittest.main()
