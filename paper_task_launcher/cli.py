from __future__ import annotations

import argparse
import sys

from .errors import LauncherError
from .exporter import export_dataset
from .launcher import launch_task, resume_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-task",
        description=(
            "Create an isolated Git repository, import a paper's PDF or LaTeX source, launch a new "
            "Codex, Claude Code, or Kimi Code session, and record final responses plus per-turn "
            "code snapshots."
        ),
        epilog=(
            "Resume an interrupted recording with: paper-task resume --help; "
            "export it with: paper-task export --help"
        ),
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help=(
            "New/empty task directory for --web copy mode, or an existing web "
            "directory for legacy in-place --continue mode"
        ),
    )
    parser.add_argument(
        "--paper",
        required=True,
        help="Local PDF file, local LaTeX directory, arXiv ID/URL, or PDF URL",
    )
    parser.add_argument(
        "--continue",
        dest="continue_existing",
        action="store_true",
        help=(
            "Modify an existing Web without the generation prompt; combine with "
            "--web to copy a source Web, or omit --web for legacy in-place mode"
        ),
    )
    parser.add_argument(
        "--web",
        help="Existing web directory to copy into a new task workspace as its baseline",
    )
    parser.add_argument(
        "--backend",
        choices=("codex", "claude", "kimi"),
        default="codex",
        help="Interactive coding agent to launch (default: codex)",
    )
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI executable")
    parser.add_argument("--claude-bin", default="claude", help="Claude Code CLI executable")
    parser.add_argument("--kimi-bin", default="kimi", help="Kimi Code CLI executable")
    parser.add_argument(
        "--claude-model",
        help="Claude Code model name for managed token-file authentication",
    )
    parser.add_argument(
        "--model",
        help="Modification model to use with --web for Codex, Claude Code, or Kimi Code",
    )
    parser.add_argument(
        "--token-file",
        help=(
            "File containing Boyue token candidates as NAME=value "
            "(default: token_pool/.env.token in the project directory)"
        ),
    )
    parser.add_argument(
        "--boyue-url",
        help=(
            "Boyue API root URL for managed Claude, Codex, or Kimi authentication "
            "(default for managed tasks: http://35.220.164.252:3888)"
        ),
    )
    parser.add_argument(
        "--claude-base-url",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Prepare the isolated repository without launching the selected agent",
    )
    return parser


def build_export_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-task export",
        description="Export a recorded task as a self-contained, analysis-ready dataset.",
    )
    parser.add_argument("--workspace", required=True, help="Recorded task directory")
    parser.add_argument(
        "--output",
        help="New or empty export directory (default: <workspace>/out_data)",
    )
    parser.add_argument(
        "--no-paper-source",
        action="store_true",
        help="Do not copy the imported paper source into the dataset",
    )
    return parser


def build_resume_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-task resume",
        description="Resume the recorded Codex, Claude Code, or Kimi Code session.",
    )
    parser.add_argument("--workspace", required=True, help="Existing recorded task directory")
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI executable")
    parser.add_argument("--claude-bin", default="claude", help="Claude Code CLI executable")
    parser.add_argument("--kimi-bin", default="kimi", help="Kimi Code CLI executable")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    try:
        if arguments and arguments[0] == "export":
            args = build_export_parser().parse_args(arguments[1:])
            export_dataset(
                args.workspace,
                args.output,
                include_paper_source=not args.no_paper_source,
                progress=lambda message: print(f"[paper-task] {message}", flush=True),
            )
            return 0
        if arguments and arguments[0] == "resume":
            args = build_resume_parser().parse_args(arguments[1:])
            return resume_task(
                args.workspace,
                codex_bin=args.codex_bin,
                claude_bin=args.claude_bin,
                kimi_bin=args.kimi_bin,
            )
        args = build_parser().parse_args(arguments)
        return launch_task(
            args.workspace,
            args.paper,
            backend=args.backend,
            codex_bin=args.codex_bin,
            claude_bin=args.claude_bin,
            kimi_bin=args.kimi_bin,
            prepare_only=args.prepare_only,
            continue_existing=args.continue_existing,
            web_source=args.web,
            model=args.model,
            claude_model=args.claude_model,
            token_file=args.token_file,
            boyue_url=args.boyue_url,
            claude_base_url=args.claude_base_url,
        )
    except LauncherError as exc:
        print(f"paper-task: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
