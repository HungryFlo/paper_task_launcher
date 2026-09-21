from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paper_task_launcher.errors import LauncherError
from paper_task_launcher.task_locator import (
    ensure_default_output_root,
    locate_paper_task,
    normalize_paper_id,
    workspace_for_paper_id,
)


class TaskLocatorTests(unittest.TestCase):
    def _annotator(self, root: Path, name: str) -> Path:
        annotator = root / name
        for wave in range(1, 5):
            (annotator / f"wave{wave}").mkdir(parents=True)
        (annotator / "webs").mkdir()
        return annotator

    def test_locates_exact_paper_and_web_with_default_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            annotator = self._annotator(data, "标注者甲")
            paper = annotator / "wave3" / "4_Example paper.pdf"
            paper.write_bytes(b"pdf")
            (annotator / "wave1" / "45_Not paper four.pdf").write_bytes(b"pdf")
            web = annotator / "webs" / "4_web"
            web.mkdir()
            (web / "index.html").write_text("ok", encoding="utf-8")

            located = locate_paper_task(
                "004",
                data_root=data,
                output_root=root / "out",
            )

            self.assertEqual(located.paper_id, "4")
            self.assertEqual(located.annotator, "标注者甲")
            self.assertEqual(located.paper, paper.resolve())
            self.assertEqual(located.web, web.resolve())
            self.assertEqual(located.default_workspace, (root / "out" / "4").resolve())

    def test_rejects_missing_matching_web(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            annotator = self._annotator(data, "标注者甲")
            (annotator / "wave1" / "4_Paper.pdf").write_bytes(b"pdf")

            with self.assertRaisesRegex(LauncherError, "matching webs/<ID>_web missing"):
                locate_paper_task("4", data_root=data, output_root=root / "out")

    def test_rejects_same_id_across_annotators(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            for name in ("标注者甲", "标注者乙"):
                annotator = self._annotator(data, name)
                (annotator / "wave1" / "4_Paper.pdf").write_bytes(b"pdf")
                (annotator / "webs" / "4_web").mkdir()

            with self.assertRaisesRegex(LauncherError, "ambiguous across annotators"):
                locate_paper_task("4", data_root=data, output_root=root / "out")

    def test_normalizes_id_and_creates_only_output_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "new" / "out"
            self.assertEqual(normalize_paper_id("004"), "4")
            self.assertEqual(workspace_for_paper_id("004", output_root=root), root / "4")
            self.assertEqual(ensure_default_output_root(root), root)
            self.assertTrue(root.is_dir())
            self.assertFalse((root / "4").exists())

    def test_rejects_invalid_id(self):
        for value in ("", "0", "-1", "4_web", "四"):
            with self.subTest(value=value):
                with self.assertRaises(LauncherError):
                    normalize_paper_id(value)


if __name__ == "__main__":
    unittest.main()
