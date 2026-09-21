from __future__ import annotations

import unittest
from contextlib import redirect_stderr
from io import StringIO

from paper_task_launcher.cli import (
    build_export_parser,
    build_parser,
    build_resume_parser,
)


class CliTests(unittest.TestCase):
    def test_parses_managed_claude_options(self):
        args = build_parser().parse_args(
            [
                "--backend",
                "claude",
                "--claude-model",
                "kimi-k3",
                "--token-file",
                "/secure/tokens.env",
                "--claude-base-url",
                "https://provider.example",
                "--workspace",
                "task",
                "--paper",
                "paper.pdf",
            ]
        )
        self.assertEqual(args.claude_model, "kimi-k3")
        self.assertEqual(args.token_file, "/secure/tokens.env")
        self.assertEqual(args.claude_base_url, "https://provider.example")

    def test_resume_has_no_provider_overrides(self):
        parser = build_resume_parser()
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    ["--workspace", "task", "--claude-model", "different-model"]
                )

    def test_export_output_is_optional(self):
        args = build_export_parser().parse_args(["--workspace", "task"])
        self.assertIsNone(args.output)

    def test_parses_web_copy_boyue_options(self):
        args = build_parser().parse_args(
            [
                "--web",
                "source-web",
                "--workspace",
                "task",
                "--paper",
                "paper.pdf",
                "--backend",
                "codex",
                "--model",
                "gpt-modifier",
                "--boyue-url",
                "https://boyue.example",
                "--token-file",
                "tokens.env",
            ]
        )
        self.assertEqual(args.web, "source-web")
        self.assertEqual(args.model, "gpt-modifier")
        self.assertEqual(args.boyue_url, "https://boyue.example")
        self.assertIsNone(args.claude_base_url)

    def test_web_and_continue_can_be_combined(self):
        args = build_parser().parse_args(
            [
                "--web",
                "source-web",
                "--continue",
                "--workspace",
                "task",
                "--paper",
                "paper.pdf",
            ]
        )
        self.assertTrue(args.continue_existing)
        self.assertEqual(args.web, "source-web")

    def test_parses_kimi_backend_and_binary(self):
        args = build_parser().parse_args(
            [
                "--backend",
                "kimi",
                "--kimi-bin",
                "/opt/kimi",
                "--model",
                "kimi-k3",
                "--web",
                "source-web",
                "--workspace",
                "task",
                "--paper",
                "paper.pdf",
            ]
        )
        self.assertEqual(args.backend, "kimi")
        self.assertEqual(args.kimi_bin, "/opt/kimi")

        resume = build_resume_parser().parse_args(
            ["--workspace", "task", "--kimi-bin", "/opt/kimi"]
        )
        self.assertEqual(resume.kimi_bin, "/opt/kimi")


if __name__ == "__main__":
    unittest.main()
