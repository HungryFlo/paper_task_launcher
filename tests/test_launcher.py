from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_task_launcher.errors import LauncherError
from paper_task_launcher.git_history import SnapshotStore
from paper_task_launcher.launcher import launch_task, prepare_task, validate_empty_workspace


class LauncherTests(unittest.TestCase):
    def test_rejects_nonempty_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "task"
            workspace.mkdir()
            (workspace / "file").write_text("x", encoding="utf-8")
            with self.assertRaises(LauncherError):
                validate_empty_workspace(str(workspace))

    def test_prepare_and_snapshot_local_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = root / "paper"
            paper.mkdir()
            (paper / "main.tex").write_text("paper", encoding="utf-8")
            workspace = root / "task"
            progress = []
            task = prepare_task(str(workspace), str(paper), progress=progress.append)
            self.assertTrue((workspace / ".git").is_dir())
            self.assertTrue((workspace / "paper-source" / "main.tex").exists())
            self.assertIn("/paper-source/", (workspace / ".gitignore").read_text())
            (workspace / "implementation.py").write_text("print('ok')\n", encoding="utf-8")
            store = SnapshotStore(workspace, task.recording_id)
            snapshot = store.capture("test")
            self.assertTrue(snapshot["commit"])
            self.assertTrue(any("论文源码导入完成" in message for message in progress))
            self.assertTrue(any("基线快照" in message for message in progress))

    def test_failed_preparation_cleans_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "task"
            with patch(
                "paper_task_launcher.launcher.import_paper",
                side_effect=LauncherError("network failed"),
            ):
                with self.assertRaises(LauncherError):
                    prepare_task(str(workspace), "2608.15089")
            self.assertTrue(workspace.is_dir())
            self.assertEqual(list(workspace.iterdir()), [])

    def test_full_launch_with_fake_codex_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = root / "paper"
            paper.mkdir()
            (paper / "main.tex").write_text("paper", encoding="utf-8")
            workspace = root / "task"
            codex_home = root / "codex-home"
            fake = root / "fake-codex"
            fake.write_text(
                """#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
workspace = Path(sys.argv[sys.argv.index('--cd') + 1]).resolve()
prompt = sys.argv[-1]
log = Path(os.environ['CODEX_HOME']) / 'sessions' / '2026' / '01' / '01' / 'rollout-test.jsonl'
log.parent.mkdir(parents=True, exist_ok=True)
records = [
  {'timestamp':'2026-01-01T00:00:00Z','ordinal':0,'type':'session_meta','payload':{'session_id':'session-test','cwd':str(workspace),'cli_version':'test'}},
  {'timestamp':'2026-01-01T00:00:01Z','ordinal':1,'type':'event_msg','payload':{'type':'task_started','turn_id':'turn-test'}},
  {'timestamp':'2026-01-01T00:00:02Z','ordinal':2,'type':'event_msg','payload':{'type':'item_completed','turn_id':'turn-test','item':{'type':'UserMessage','content':[{'type':'text','text':prompt}]}}},
  {'timestamp':'2026-01-01T00:00:03Z','ordinal':3,'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn-test','last_agent_message':'done','completed_at':'2026-01-01T00:00:03Z'}},
]
with log.open('w') as handle:
    for record in records:
        handle.write(json.dumps(record) + '\\n')
        handle.flush()
time.sleep(0.4)
""",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                code = launch_task(
                    str(workspace), str(paper), codex_bin=str(fake), progress=None
                )
            self.assertEqual(code, 0)
            transcript = [
                json.loads(line)
                for line in (workspace / ".recording" / "transcript.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(transcript), 1)
            self.assertEqual(transcript[0]["final_response"], "done")
            self.assertIn("论文 LaTeX 源码", transcript[0]["user_input"])
            self.assertIn("前端网页", transcript[0]["user_input"])
            self.assertIn("对用户友好", transcript[0]["user_input"])
            self.assertIn("保留论文关键信息", transcript[0]["user_input"])
            self.assertIn("渐进方式", transcript[0]["user_input"])
            self.assertIn("项目规则", transcript[0]["user_input"])
            self.assertNotIn("复现", transcript[0]["user_input"])
            self.assertNotIn("术语表", transcript[0]["user_input"])
            self.assertNotIn("响应式布局", transcript[0]["user_input"])
            manifest = json.loads((workspace / ".recording" / "manifest.json").read_text())
            self.assertEqual(manifest["session_id"], "session-test")
            self.assertEqual(manifest["turn_count"], 1)
            self.assertEqual(manifest["state"], "completed")


if __name__ == "__main__":
    unittest.main()
