from __future__ import annotations

import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from paper_task_launcher.cli import (
    build_export_parser,
    build_parser,
    build_resume_parser,
    main,
)
from paper_task_launcher.task_locator import LocatedPaperTask


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

    def test_resume_and_export_accept_paper_id(self):
        resume = build_resume_parser().parse_args(["--paperID", "4"])
        exported = build_export_parser().parse_args(["--paper-id", "4"])
        self.assertEqual(resume.paper_id, "4")
        self.assertEqual(exported.paper_id, "4")
        self.assertIsNone(resume.workspace)
        self.assertIsNone(exported.workspace)

    def test_launch_parser_accepts_minimal_paper_id_command(self):
        args = build_parser().parse_args(
            [
                "--paperID",
                "4",
                "--model",
                "claude-opus-4-6",
                "--backend",
                "claude",
            ]
        )
        self.assertEqual(args.paper_id, "4")
        self.assertIsNone(args.paper)
        self.assertIsNone(args.web)
        self.assertIsNone(args.workspace)

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

    @patch("paper_task_launcher.cli.ensure_default_output_root")
    @patch("paper_task_launcher.cli.launch_task", return_value=0)
    @patch("paper_task_launcher.cli.locate_paper_task")
    def test_main_resolves_minimal_paper_id_launch(
        self,
        locate,
        launch,
        ensure_output,
    ):
        locate.return_value = LocatedPaperTask(
            paper_id="4",
            annotator="刘毅",
            paper=Path("/project/data/刘毅/wave1/4_Paper.pdf"),
            web=Path("/project/data/刘毅/webs/4_web"),
            default_workspace=Path("/project/out/4"),
        )

        code = main(
            [
                "--paperID",
                "4",
                "--model",
                "claude-opus-4-6",
                "--backend",
                "claude",
            ]
        )

        self.assertEqual(code, 0)
        ensure_output.assert_called_once_with()
        positional, keyword = launch.call_args
        self.assertEqual(positional, ("/project/out/4", "/project/data/刘毅/wave1/4_Paper.pdf"))
        self.assertEqual(keyword["web_source"], "/project/data/刘毅/webs/4_web")
        self.assertTrue(keyword["continue_existing"])
        self.assertEqual(keyword["boyue_url"], "http://35.220.164.252:3888")
        self.assertEqual(keyword["paper_id"], "4")
        self.assertEqual(keyword["annotator"], "刘毅")

    @patch("paper_task_launcher.cli.resume_task", return_value=0)
    @patch(
        "paper_task_launcher.cli.workspace_for_paper_id",
        return_value=Path("/project/out/4"),
    )
    def test_main_resolves_resume_paper_id(self, workspace_for_id, resume):
        code = main(["resume", "--paperID", "4"])
        self.assertEqual(code, 0)
        workspace_for_id.assert_called_once_with("4")
        self.assertEqual(resume.call_args.args[0], "/project/out/4")


if __name__ == "__main__":
    unittest.main()
