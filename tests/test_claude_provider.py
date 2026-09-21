from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_task_launcher.claude_provider import (
    load_tokens,
    managed_config,
    profile_environment,
    select_token,
    session_environment,
    write_private_json,
)
from paper_task_launcher.errors import LauncherError


class ClaudeProviderTests(unittest.TestCase):
    def test_loads_labeled_tokens_and_rejects_invalid_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = root / "tokens.env"
            valid.write_text(
                "# candidates\nexport FIRST='one'\n\nSECOND=two\n",
                encoding="utf-8",
            )
            candidates = load_tokens(valid)
            self.assertEqual([item.label for item in candidates], ["FIRST", "SECOND"])
            self.assertEqual([item.value for item in candidates], ["one", "two"])

            duplicate = root / "duplicate.env"
            duplicate.write_text("KEY=one\nKEY=two\n", encoding="utf-8")
            with self.assertRaisesRegex(LauncherError, "duplicate label"):
                load_tokens(duplicate)

            malformed = root / "malformed.env"
            malformed.write_text("not-a-pair\n", encoding="utf-8")
            with self.assertRaisesRegex(LauncherError, "NAME=value"):
                load_tokens(malformed)

    def test_managed_options_are_all_or_none_and_claude_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            token_file = Path(temporary) / "tokens.env"
            token_file.write_text("KEY=value\n", encoding="utf-8")
            self.assertIsNone(
                managed_config(
                    backend="claude",
                    model=None,
                    token_file=None,
                    base_url=None,
                )
            )
            with self.assertRaisesRegex(LauncherError, "requires all"):
                managed_config(
                    backend="claude",
                    model=None,
                    token_file=str(token_file),
                    base_url="https://example.test",
                )
            with self.assertRaisesRegex(LauncherError, "--backend claude"):
                managed_config(
                    backend="codex",
                    model="kimi-k3",
                    token_file=str(token_file),
                    base_url="https://example.test",
                )

            config = managed_config(
                backend="claude",
                model="kimi-k3",
                token_file=str(token_file),
                base_url="https://example.test/",
            )
            self.assertEqual(config["profile"], "kimi")
            self.assertEqual(config["base_url"], "https://example.test")
            self.assertEqual(config["token_file"], str(token_file.resolve()))

            claude_config = managed_config(
                backend="claude",
                model="claude-sonnet-test",
                token_file=str(token_file),
                base_url="https://example.test",
            )
            self.assertEqual(claude_config["profile"], "claude")

    def test_selects_first_successful_token_without_persisting_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token_file = root / "tokens.env"
            token_file.write_text("FIRST=secret-one\nSECOND=secret-two\n", encoding="utf-8")
            fake = root / "claude"
            fake.write_text(
                """#!/usr/bin/env python3
import json, os, sys, time
if os.environ.get('ANTHROPIC_AUTH_TOKEN') == 'secret-one':
    time.sleep(5)
print(json.dumps({'result': 'OK'}))
""",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            config = {
                "mode": "token_file",
                "profile": "kimi",
                "model": "kimi-k3",
                "base_url": "https://example.test",
                "token_file": str(token_file),
            }
            progress: list[str] = []
            started = time.monotonic()
            selected = select_token(config, str(fake), progress=progress.append)
            elapsed = time.monotonic() - started
            self.assertEqual(selected.label, "SECOND")
            self.assertLess(elapsed, 2)
            self.assertFalse(any("secret-one" in line for line in progress))
            self.assertFalse(any("secret-two" in line for line in progress))

    def test_kimi_environment_and_private_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {
                "profile": "kimi",
                "model": "kimi-k3",
                "base_url": "https://example.test",
            }
            profile = profile_environment(config, "secret-value")
            self.assertEqual(profile["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "kimi-k3")
            self.assertEqual(profile["ANTHROPIC_DEFAULT_SONNET_MODEL"], "kimi-k3")
            with patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://proxy.invalid:8080",
                    "HTTPS_PROXY": "http://proxy.invalid:8080",
                    "ALL_PROXY": "socks5://proxy.invalid:1080",
                },
            ):
                env = session_environment(config, "secret-value", root / "config")
            self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "secret-value")
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], str((root / "config").resolve()))
            self.assertNotIn("HTTP_PROXY", env)
            self.assertNotIn("HTTPS_PROXY", env)
            self.assertNotIn("ALL_PROXY", env)

            settings = root / "config" / "settings.json"
            write_private_json(settings, {"hooks": {}})
            self.assertEqual(json.loads(settings.read_text()), {"hooks": {}})
            self.assertEqual(os.stat(settings).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(settings.parent).st_mode & 0o777, 0o700)

    def test_probe_timeout_reports_failure_without_response_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token_file = root / "tokens.env"
            token_file.write_text("ONLY=timeout-secret\n", encoding="utf-8")
            fake = root / "claude"
            fake.write_text(
                "#!/usr/bin/env python3\nimport time\ntime.sleep(1)\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            config = {
                "mode": "token_file",
                "profile": "claude",
                "model": "claude-test",
                "base_url": "https://example.test",
                "token_file": str(token_file),
            }
            progress: list[str] = []
            with self.assertRaisesRegex(LauncherError, "No usable token"):
                select_token(
                    config,
                    str(fake),
                    timeout=0.01,
                    progress=progress.append,
                )
            self.assertTrue(any("timeout" in line for line in progress))
            self.assertFalse(any("timeout-secret" in line for line in progress))


if __name__ == "__main__":
    unittest.main()
