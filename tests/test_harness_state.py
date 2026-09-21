from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_task_launcher.harness_state import (
    ManagedHarnessHome,
    RUNTIME_ROOT_ENV,
    recover_harness_home,
)


class ManagedHarnessHomeTests(unittest.TestCase):
    def test_round_trips_complete_task_home_through_local_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            persistent = root / "workspace" / ".recording" / "codex-home"
            persistent.mkdir(parents=True)
            (persistent / "config.toml").write_text("model = 'test'\n")
            runtime_root = root / "runtime"

            with patch.dict(
                "os.environ", {RUNTIME_ROOT_ENV: str(runtime_root)}
            ):
                with ManagedHarnessHome(
                    persistent,
                    "rec-test",
                    require_mmap=True,
                ) as home:
                    self.assertNotEqual(home.runtime_home, persistent)
                    self.assertEqual(
                        (home.runtime_home / "config.toml").read_text(),
                        "model = 'test'\n",
                    )
                    log = home.runtime_home / "sessions" / "session.jsonl"
                    log.parent.mkdir(parents=True)
                    log.write_text("first\n")
                    self.assertEqual(
                        home.persistent_path_for(log),
                        persistent / "sessions" / "session.jsonl",
                    )
                    runtime_task_dir = home.runtime_task_dir

                self.assertFalse(runtime_task_dir.exists())
                self.assertEqual(
                    (persistent / "sessions" / "session.jsonl").read_text(),
                    "first\n",
                )

                with ManagedHarnessHome(persistent, "rec-test") as home:
                    log = home.runtime_home / "sessions" / "session.jsonl"
                    self.assertEqual(log.read_text(), "first\n")
                    log.write_text("first\nsecond\n")
                    self.assertEqual(
                        home.runtime_path_for(
                            persistent / "sessions" / "session.jsonl"
                        ),
                        log,
                    )

            self.assertEqual(
                (persistent / "sessions" / "session.jsonl").read_text(),
                "first\nsecond\n",
            )

    def test_recovers_previous_home_after_interrupted_swap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            persistent = root / "kimi-home"
            persistent.mkdir()
            (persistent / "state.json").write_text("saved")
            backup = root / ".kimi-home.sync-backup"
            persistent.rename(backup)
            staging = root / ".kimi-home.sync-staging"
            staging.mkdir()
            (staging / "partial").write_text("partial")

            recover_harness_home(persistent)

            self.assertEqual((persistent / "state.json").read_text(), "saved")
            self.assertFalse(backup.exists())
            self.assertFalse(staging.exists())


if __name__ == "__main__":
    unittest.main()
