from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_task_launcher.errors import LauncherError
from paper_task_launcher.git_history import SnapshotStore
from paper_task_launcher.launcher import (
    launch_task,
    prepare_task,
    resume_task,
    validate_empty_workspace,
)
from paper_task_launcher.util import run_checked


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
log = Path(os.environ['CODEX_HOME']) / 'sessions' / '2026' / '01' / '01' / 'rollout-test.jsonl'
log.parent.mkdir(parents=True, exist_ok=True)
if len(sys.argv) > 1 and sys.argv[1] == 'resume':
    records = [
      {'timestamp':'2026-01-01T00:01:01Z','ordinal':4,'type':'event_msg','payload':{'type':'task_started','turn_id':'turn-resume'}},
      {'timestamp':'2026-01-01T00:01:02Z','ordinal':5,'type':'event_msg','payload':{'type':'item_completed','turn_id':'turn-resume','item':{'type':'UserMessage','content':[{'type':'text','text':'follow up'}]}}},
      {'timestamp':'2026-01-01T00:01:03Z','ordinal':6,'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn-resume','last_agent_message':'resumed done','completed_at':'2026-01-01T00:01:03Z'}},
    ]
    mode = 'a'
    (workspace / 'resumed.py').write_text("print('resumed')\\n")
else:
    prompt = sys.argv[-1]
    records = [
      {'timestamp':'2026-01-01T00:00:00Z','ordinal':0,'type':'session_meta','payload':{'session_id':'session-test','cwd':str(workspace),'cli_version':'test'}},
      {'timestamp':'2026-01-01T00:00:01Z','ordinal':1,'type':'event_msg','payload':{'type':'task_started','turn_id':'turn-test'}},
      {'timestamp':'2026-01-01T00:00:02Z','ordinal':2,'type':'event_msg','payload':{'type':'item_completed','turn_id':'turn-test','item':{'type':'UserMessage','content':[{'type':'text','text':prompt}]}}},
      {'timestamp':'2026-01-01T00:00:03Z','ordinal':3,'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn-test','last_agent_message':'done','completed_at':'2026-01-01T00:00:03Z'}},
    ]
    mode = 'w'
with log.open(mode) as handle:
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
            self.assertIn("高覆盖率", transcript[0]["user_input"])
            self.assertIn("尽量不遗漏论文中的内容与细节", transcript[0]["user_input"])
            self.assertIn("渐进信息展现方式", transcript[0]["user_input"])
            self.assertIn("项目规则", transcript[0]["user_input"])
            self.assertNotIn("复现", transcript[0]["user_input"])
            self.assertNotIn("术语表", transcript[0]["user_input"])
            self.assertNotIn("响应式布局", transcript[0]["user_input"])
            manifest = json.loads((workspace / ".recording" / "manifest.json").read_text())
            self.assertEqual(manifest["session_id"], "session-test")
            self.assertEqual(manifest["turn_count"], 1)
            self.assertEqual(manifest["state"], "completed")

            # Older recordings have no saved log offset. Resume must safely rescan
            # their log without duplicating the already recorded turn.
            manifest.pop("session_log_offset", None)
            (workspace / ".recording" / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                code = resume_task(str(workspace), codex_bin=str(fake), progress=None)
            self.assertEqual(code, 0)
            transcript = [
                json.loads(line)
                for line in (workspace / ".recording" / "transcript.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(transcript), 2)
            self.assertEqual(transcript[1]["turn_index"], 2)
            self.assertEqual(transcript[1]["user_input"], "follow up")
            self.assertEqual(transcript[1]["final_response"], "resumed done")
            self.assertEqual(
                transcript[1]["snapshot"]["index"], 2
            )
            self.assertEqual(
                run_checked(
                    ["git", "rev-parse", f"{transcript[1]['snapshot']['commit']}^"],
                    cwd=workspace,
                ),
                transcript[0]["snapshot"]["commit"],
            )
            manifest = json.loads((workspace / ".recording" / "manifest.json").read_text())
            self.assertEqual(manifest["turn_count"], 2)
            self.assertEqual(manifest["resume_count"], 1)
            self.assertEqual(manifest["resume_history"][0]["turn_count_before"], 1)
            self.assertEqual(manifest["resume_history"][0]["turn_count_after"], 2)

    def test_full_launch_with_fake_claude_hooks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = root / "paper"
            paper.mkdir()
            (paper / "main.tex").write_text("paper", encoding="utf-8")
            workspace = root / "task"
            fake = root / "fake-claude"
            fake.write_text(
                """#!/usr/bin/env python3
import json, subprocess, sys
from pathlib import Path
settings_path = Path(sys.argv[sys.argv.index('--settings') + 1])
settings = json.loads(settings_path.read_text())
workspace = Path.cwd()
common = {'session_id':'claude-session','transcript_path':'/tmp/claude.jsonl','cwd':str(workspace)}
resuming = '--resume' in sys.argv
prompt = 'claude follow up' if resuming else sys.argv[-1]
prompt_id = 'prompt-2' if resuming else 'prompt-1'
events = [
  {**common,'hook_event_name':'SessionStart','model':'claude-test'},
  {**common,'hook_event_name':'UserPromptSubmit','prompt_id':prompt_id,'prompt':prompt},
]
for event in events:
    hook = settings['hooks'][event['hook_event_name']][0]['hooks'][0]
    subprocess.run([hook['command'], *hook['args']], input=json.dumps(event), text=True, check=True)
(workspace / ('claude-resumed.py' if resuming else 'claude-app.py')).write_text("print('ok')\\n")
stop = {**common,'hook_event_name':'Stop','prompt_id':prompt_id,'last_assistant_message':('claude resumed' if resuming else 'claude done'),'stop_hook_active':False}
hook = settings['hooks']['Stop'][0]['hooks'][0]
subprocess.run([hook['command'], *hook['args']], input=json.dumps(stop), text=True, check=True)
""",
                encoding="utf-8",
            )
            fake.chmod(0o755)

            code = launch_task(
                str(workspace),
                str(paper),
                backend="claude",
                claude_bin=str(fake),
                progress=None,
            )

            self.assertEqual(code, 0)
            transcript = json.loads(
                (workspace / ".recording" / "transcript.jsonl").read_text()
            )
            self.assertIn("论文 LaTeX 源码", transcript["user_input"])
            self.assertEqual(transcript["final_response"], "claude done")
            manifest = json.loads(
                (workspace / ".recording" / "manifest.json").read_text()
            )
            self.assertEqual(manifest["backend"], "claude")
            self.assertEqual(manifest["session_id"], "claude-session")
            self.assertEqual(manifest["turn_count"], 1)
            self.assertEqual(manifest["state"], "completed")

            code = resume_task(
                str(workspace), claude_bin=str(fake), progress=None
            )
            self.assertEqual(code, 0)
            transcript = [
                json.loads(line)
                for line in (workspace / ".recording" / "transcript.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(transcript), 2)
            self.assertEqual(transcript[1]["user_input"], "claude follow up")
            self.assertEqual(transcript[1]["final_response"], "claude resumed")
            manifest = json.loads(
                (workspace / ".recording" / "manifest.json").read_text()
            )
            self.assertEqual(manifest["turn_count"], 2)
            self.assertEqual(manifest["resume_count"], 1)


if __name__ == "__main__":
    unittest.main()
