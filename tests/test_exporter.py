from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paper_task_launcher.errors import LauncherError
from paper_task_launcher.exporter import export_dataset
from paper_task_launcher.git_history import SnapshotStore
from paper_task_launcher.launcher import prepare_task
from paper_task_launcher.util import append_jsonl, atomic_json


class ExporterTests(unittest.TestCase):
    def test_exports_analysis_ready_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = root / "paper"
            paper.mkdir()
            (paper / "main.tex").write_text("paper", encoding="utf-8")
            workspace = root / "task"
            task = prepare_task(str(workspace), str(paper), progress=None)

            (workspace / "app.py").write_text("print('one')\n", encoding="utf-8")
            snapshots = SnapshotStore(workspace, task.recording_id)
            snapshots.parent = task.manifest["baseline_snapshot"]["commit"]
            first = snapshots.capture("turn-0001", turn_id="remote-turn-1")
            turn = {
                "turn_id": "remote-turn-1",
                "turn_index": 1,
                "user_input": "question",
                "final_response": "answer",
                "snapshot": first,
            }
            append_jsonl(workspace / ".recording" / "transcript.jsonl", turn)
            task.manifest["turn_count"] = 1
            task.manifest["latest_snapshot"] = first
            atomic_json(workspace / ".recording" / "manifest.json", task.manifest)

            output = root / "dataset"
            result = export_dataset(str(workspace), str(output), progress=None)

            self.assertEqual(result, output)
            dataset = json.loads((output / "dataset.json").read_text())
            self.assertEqual(dataset["schema"], "paper-task-dataset-v1")
            self.assertEqual(dataset["turn_count"], 1)
            exported_turn = json.loads((output / "turns.jsonl").read_text())
            self.assertEqual(exported_turn["code_path"], "versions/turn-0001")
            self.assertEqual(exported_turn["user_input"], "question")
            self.assertEqual(
                (output / "versions" / "turn-0001" / "app.py").read_text(),
                "print('one')\n",
            )
            self.assertTrue((output / "versions" / "baseline" / ".gitignore").is_file())
            self.assertEqual((output / "paper-source" / "main.tex").read_text(), "paper")
            self.assertFalse((output / ".git").exists())

    def test_refuses_nonempty_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = root / "paper"
            paper.mkdir()
            (paper / "main.tex").write_text("paper", encoding="utf-8")
            workspace = root / "task"
            prepare_task(str(workspace), str(paper), progress=None)
            output = root / "dataset"
            output.mkdir()
            (output / "keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(LauncherError):
                export_dataset(str(workspace), str(output), progress=None)


if __name__ == "__main__":
    unittest.main()
