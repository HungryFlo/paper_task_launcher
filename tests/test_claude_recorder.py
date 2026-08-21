from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paper_task_launcher.claude_recorder import (
    build_claude_settings,
    handle_claude_hook,
)
from paper_task_launcher.launcher import prepare_task


class ClaudeRecorderTests(unittest.TestCase):
    def test_records_prompt_response_and_snapshot_from_hooks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = root / "paper"
            paper.mkdir()
            (paper / "main.tex").write_text("paper", encoding="utf-8")
            workspace = root / "task"
            task = prepare_task(
                str(workspace), str(paper), backend="claude", progress=None
            )
            recording_dir = workspace / ".recording"
            common = {
                "session_id": "claude-session",
                "transcript_path": str(root / "claude-transcript.jsonl"),
                "cwd": str(workspace),
            }

            handle_claude_hook(
                workspace,
                recording_dir,
                {**common, "hook_event_name": "SessionStart", "model": "claude-test"},
            )
            handle_claude_hook(
                workspace,
                recording_dir,
                {
                    **common,
                    "hook_event_name": "UserPromptSubmit",
                    "prompt_id": "prompt-1",
                    "prompt": "question",
                },
            )
            (workspace / "app.py").write_text("print('claude')\n", encoding="utf-8")
            handle_claude_hook(
                workspace,
                recording_dir,
                {
                    **common,
                    "hook_event_name": "Stop",
                    "prompt_id": "prompt-1",
                    "last_assistant_message": "final answer",
                    "stop_hook_active": False,
                },
            )

            transcript = json.loads((recording_dir / "transcript.jsonl").read_text())
            self.assertEqual(transcript["user_input"], "question")
            self.assertEqual(transcript["final_response"], "final answer")
            self.assertEqual(transcript["turn_index"], 1)
            self.assertTrue(transcript["snapshot"]["commit"])
            manifest = json.loads((recording_dir / "manifest.json").read_text())
            self.assertEqual(manifest["backend"], "claude")
            self.assertEqual(manifest["session_id"], "claude-session")
            self.assertEqual(manifest["claude_model"], "claude-test")
            self.assertEqual(manifest["turn_count"], 1)

    def test_settings_use_official_turn_hooks(self):
        workspace = Path("/tmp/task").resolve()
        settings = build_claude_settings(workspace, workspace / ".recording")
        self.assertEqual(
            set(settings["hooks"]), {"SessionStart", "UserPromptSubmit", "Stop"}
        )
        for groups in settings["hooks"].values():
            hook = groups[0]["hooks"][0]
            self.assertEqual(hook["type"], "command")
            self.assertIn("paper_task_launcher.claude_recorder", hook["args"][1])


if __name__ == "__main__":
    unittest.main()
