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
            "Codex or Claude Code session, and record final responses plus per-turn code snapshots."
        ),
        epilog=(
            "Resume an interrupted recording with: paper-task resume --help; "
            "export it with: paper-task export --help"
        ),
    )
    parser.add_argument("--workspace", required=True, help="New or empty task directory")
    parser.add_argument(
        "--paper",
        required=True,
        help="Local PDF file, local LaTeX directory, arXiv ID/URL, or PDF URL",
    )
    parser.add_argument(
        "--backend",
        choices=("codex", "claude"),
        default="codex",
        help="Interactive coding agent to launch (default: codex)",
    )
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI executable")
    parser.add_argument("--claude-bin", default="claude", help="Claude Code CLI executable")
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
    parser.add_argument("--output", required=True, help="New or empty export directory")
    parser.add_argument(
        "--no-paper-source",
        action="store_true",
        help="Do not copy the imported paper source into the dataset",
    )
    return parser


def build_resume_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-task resume",
        description="Resume the recorded Codex or Claude Code session in an existing task.",
    )
    parser.add_argument("--workspace", required=True, help="Existing recorded task directory")
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI executable")
    parser.add_argument("--claude-bin", default="claude", help="Claude Code CLI executable")
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
            )
        args = build_parser().parse_args(arguments)
        return launch_task(
            args.workspace,
            args.paper,
            backend=args.backend,
            codex_bin=args.codex_bin,
            claude_bin=args.claude_bin,
            prepare_only=args.prepare_only,
        )
    except LauncherError as exc:
        print(f"paper-task: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
