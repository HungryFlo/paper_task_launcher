from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from paper_task_launcher.errors import LauncherError
from paper_task_launcher.paper import (
    _extract_source_archive,
    _fetch_bytes,
    extract_arxiv_id,
    import_paper,
)


class PaperTests(unittest.TestCase):
    def test_network_falls_back_after_retries(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return b"ok"

        progress = []
        with patch(
            "paper_task_launcher.paper._open",
            side_effect=[
                urllib.error.URLError("tls timeout"),
                urllib.error.URLError("tls timeout"),
                Response(),
            ],
        ), patch("paper_task_launcher.paper.time.sleep"):
            data, used_url = _fetch_bytes(
                ["https://primary", "http://fallback"], progress=progress.append
            )
        self.assertEqual(data, b"ok")
        self.assertEqual(used_url, "http://fallback")
        self.assertTrue(any("重试" in message for message in progress))
        self.assertTrue(any("备用端点" in message for message in progress))

    def test_extract_arxiv_id(self):
        self.assertEqual(extract_arxiv_id("2401.01234"), "2401.01234")
        self.assertEqual(
            extract_arxiv_id("https://arxiv.org/pdf/2401.01234v2.pdf"),
            "2401.01234v2",
        )
        self.assertEqual(
            extract_arxiv_id("https://arxiv.org/abs/hep-th/9901001"),
            "hep-th/9901001",
        )
        self.assertIsNone(extract_arxiv_id("https://example.com/paper.pdf"))

    def test_local_import_excludes_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / "main.tex").write_text("hello", encoding="utf-8")
            (source / ".git").mkdir()
            (source / ".git" / "config").write_text("secret", encoding="utf-8")
            metadata = import_paper(str(source), destination)
            self.assertEqual(metadata.kind, "local_latex_directory")
            self.assertTrue((destination / "main.tex").exists())
            self.assertFalse((destination / ".git").exists())

    def test_local_import_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "main.tex").write_text("hello", encoding="utf-8")
            (source / "link").symlink_to(source / "main.tex")
            with self.assertRaises(LauncherError):
                import_paper(str(source), root / "destination")

    def test_archive_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "source.tar"
            with tarfile.open(archive, "w") as tar:
                data = b"bad"
                info = tarfile.TarInfo("../escape.tex")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            with self.assertRaises(LauncherError):
                _extract_source_archive(archive, root / "output")

    def test_archive_rejects_pdf_only_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "source"
            archive.write_bytes(b"%PDF-1.7\n")
            with self.assertRaises(LauncherError):
                _extract_source_archive(archive, root / "output")

    def test_local_source_must_be_outside_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "main.tex").write_text("paper", encoding="utf-8")
            with self.assertRaises(LauncherError):
                import_paper(str(workspace), workspace / "paper-source")

    def test_arxiv_source_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "paper-source"

            def fake_download(_url, archive, *, progress=None):
                with tarfile.open(archive, "w:gz") as tar:
                    content = b"\\documentclass{article}"
                    info = tarfile.TarInfo("main.tex")
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
                return "archive-sha", 123, "https://arxiv.org/e-print/2401.01234"

            with patch("paper_task_launcher.paper.query_arxiv_by_id") as metadata_api, patch(
                "paper_task_launcher.paper._download", side_effect=fake_download
            ):
                metadata = import_paper("2401.01234", destination)
            metadata_api.assert_not_called()
            self.assertEqual(metadata.kind, "arxiv_source")
            self.assertEqual(metadata.arxiv_id, "2401.01234")
            self.assertIsNone(metadata.title)
            self.assertIsNone(metadata.authors)
            self.assertIsNone(metadata.metadata_api_url)
            self.assertEqual(metadata.api_entry_url, "https://arxiv.org/abs/2401.01234")
            self.assertEqual(metadata.downloaded_archive_sha256, "archive-sha")
            self.assertTrue((destination / "main.tex").exists())


if __name__ == "__main__":
    unittest.main()
