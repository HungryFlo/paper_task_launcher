from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_task_launcher.kimi_provider import (
    TOKEN_ENV_KEY,
    model_alias,
    select_token,
    session_environment,
    write_config,
)


class KimiProviderTests(unittest.TestCase):
    def test_private_config_uses_environment_credential(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {
                "model": "kimi-k3",
                "base_url": "http://boyue.example",
                "api_base_url": "http://boyue.example/v1",
            }
            path = write_config(root / "kimi-home", config)
            text = path.read_text(encoding="utf-8")
            self.assertIn(f'api_key_env = "{TOKEN_ENV_KEY}"', text)
            self.assertIn('base_url = "http://boyue.example/v1"', text)
            self.assertIn('model = "kimi-k3"', text)
            self.assertNotIn("secret-value", text)
            self.assertEqual(model_alias(config), "paper-task/kimi-k3")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with patch.dict(
                os.environ,
                {"HTTP_PROXY": "http://proxy.invalid", "KIMI_API_KEY": "old"},
            ):
                env = session_environment(config, "secret-value", path.parent)
            self.assertEqual(env[TOKEN_ENV_KEY], "secret-value")
            self.assertEqual(env["KIMI_CODE_HOME"], str(path.parent.resolve()))
            self.assertNotIn("HTTP_PROXY", env)
            self.assertNotIn("KIMI_API_KEY", env)

    def test_first_success_cancels_slower_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tokens = root / "tokens.env"
            tokens.write_text("SLOW=slow\nFAST=fast\n", encoding="utf-8")
            fake = root / "kimi"
            fake.write_text(
                """#!/usr/bin/env python3
import os, time
if os.environ.get('PAPER_TASK_BOYUE_TOKEN') == 'slow':
    time.sleep(5)
print('OK')
""",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            config = {
                "mode": "boyue_token_file",
                "backend": "kimi",
                "model": "kimi-k3",
                "base_url": "http://boyue.example",
                "api_base_url": "http://boyue.example/v1",
                "token_file": str(tokens),
            }
            started = time.monotonic()
            selected = select_token(config, str(fake), progress=None)
            self.assertEqual(selected.label, "FAST")
            self.assertLess(time.monotonic() - started, 2)


if __name__ == "__main__":
    unittest.main()
