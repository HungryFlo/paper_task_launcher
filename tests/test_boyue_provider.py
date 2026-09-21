from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paper_task_launcher.boyue_provider import (
    DEFAULT_BOYUE_URL,
    codex_api_base_url,
    normalize_config,
)
from paper_task_launcher.errors import LauncherError


class BoyueProviderTests(unittest.TestCase):
    def test_default_and_codex_api_urls(self):
        self.assertEqual(DEFAULT_BOYUE_URL, "http://35.220.164.252:3888")
        self.assertEqual(
            codex_api_base_url(DEFAULT_BOYUE_URL),
            "http://35.220.164.252:3888/v1",
        )
        self.assertEqual(
            codex_api_base_url("https://gateway.example/v1/"),
            "https://gateway.example/v1",
        )

    def test_config_derives_backend_specific_api_url_and_validates_url(self):
        with tempfile.TemporaryDirectory() as temporary:
            tokens = Path(temporary) / "tokens.env"
            tokens.write_text("PRIMARY=secret\n", encoding="utf-8")
            codex = normalize_config(
                backend="codex",
                model="gpt-test",
                token_file=str(tokens),
                base_url="https://gateway.example",
            )
            claude = normalize_config(
                backend="claude",
                model="claude-test",
                token_file=str(tokens),
                base_url="https://gateway.example/",
            )
            kimi = normalize_config(
                backend="kimi",
                model="kimi-k3",
                token_file=str(tokens),
                base_url="https://gateway.example/",
            )
            self.assertEqual(codex["base_url"], "https://gateway.example")
            self.assertEqual(codex["api_base_url"], "https://gateway.example/v1")
            self.assertEqual(claude["api_base_url"], "https://gateway.example")
            self.assertEqual(kimi["api_base_url"], "https://gateway.example/v1")
            with self.assertRaisesRegex(LauncherError, "absolute http"):
                normalize_config(
                    backend="codex",
                    model="gpt-test",
                    token_file=str(tokens),
                    base_url="gateway.example",
                )
            with self.assertRaisesRegex(LauncherError, "query or fragment"):
                normalize_config(
                    backend="codex",
                    model="gpt-test",
                    token_file=str(tokens),
                    base_url="https://gateway.example?token=no",
                )


if __name__ == "__main__":
    unittest.main()
