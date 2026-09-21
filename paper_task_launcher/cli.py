from __future__ import annotations

import argparse
import shlex
import sys

from .boyue_provider import DEFAULT_BOYUE_URL
from .errors import LauncherError
from .exporter import export_dataset
from .launcher import launch_task, resume_task
from .task_locator import (
    ensure_default_output_root,
    locate_paper_task,
    workspace_for_paper_id,
)


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
        "--paperID",
        "--paper-id",
        dest="paper_id",
        help=(
            "Paper ID to resolve from data/<annotator>/wave1-wave4 and "
            "data/<annotator>/webs (recommended)"
        ),
    )
    parser.add_argument(
        "--workspace",
        help=(
            "New/empty task directory (default with --paperID: "
            "<paper_task_launcher>/out/<paperID>)"
        ),
    )
    parser.add_argument(
        "--paper",
        help=(
            "Explicit local PDF/LaTeX source, arXiv ID/URL, or PDF URL; "
            "use instead of --paperID for the legacy path-based workflow"
        ),
    )
    parser.add_argument(
        "--continue",
        dest="continue_existing",
        action="store_true",
        help=(
            "Modify an existing Web without the generation prompt; combine with "
            "--web to copy a source Web, or omit --web for legacy in-place mode. "
            "Automatically enabled by --paperID"
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
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--paperID",
        "--paper-id",
        dest="paper_id",
        help="Paper ID whose default workspace is out/<paperID>",
    )
    target.add_argument("--workspace", help="Recorded task directory")
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
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--paperID",
        "--paper-id",
        dest="paper_id",
        help="Paper ID whose default workspace is out/<paperID>",
    )
    target.add_argument("--workspace", help="Existing recorded task directory")
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI executable")
    parser.add_argument("--claude-bin", default="claude", help="Claude Code CLI executable")
    parser.add_argument("--kimi-bin", default="kimi", help="Kimi Code CLI executable")
    return parser


def _recorded_workspace(paper_id: str | None, workspace: str | None) -> str:
    if workspace is not None:
        return workspace
    assert paper_id is not None
    return str(workspace_for_paper_id(paper_id))


def _print_workspace_hint(
    workspace: str,
    *,
    paper_id: str | None,
    uses_default_workspace: bool,
) -> None:
    print(f"[paper-task] 任务 workspace：{workspace}", flush=True)
    if uses_default_workspace:
        target = f"--paperID {paper_id}"
    else:
        target = f"--workspace {shlex.quote(workspace)}"
    print(f"[paper-task] 中断后恢复：paper-task resume {target}", flush=True)
    print(f"[paper-task] 完成后导出：paper-task export {target}", flush=True)


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    try:
        if arguments and arguments[0] == "export":
            args = build_export_parser().parse_args(arguments[1:])
            workspace = _recorded_workspace(args.paper_id, args.workspace)
            export_dataset(
                workspace,
                args.output,
                include_paper_source=not args.no_paper_source,
                progress=lambda message: print(f"[paper-task] {message}", flush=True),
            )
            return 0
        if arguments and arguments[0] == "resume":
            args = build_resume_parser().parse_args(arguments[1:])
            return resume_task(
                _recorded_workspace(args.paper_id, args.workspace),
                codex_bin=args.codex_bin,
                claude_bin=args.claude_bin,
                kimi_bin=args.kimi_bin,
            )
        args = build_parser().parse_args(arguments)
        paper_id = None
        annotator = None
        uses_default_workspace = False
        if args.paper_id is not None:
            if args.paper is not None or args.web is not None:
                raise LauncherError(
                    "--paperID automatically selects the paper and Web; do not combine "
                    "it with --paper or --web"
                )
            located = locate_paper_task(args.paper_id)
            paper_id = located.paper_id
            annotator = located.annotator
            paper = str(located.paper)
            web = str(located.web)
            if args.workspace is None:
                ensure_default_output_root()
                workspace = str(located.default_workspace)
                uses_default_workspace = True
            else:
                workspace = args.workspace
            continue_existing = True
            boyue_url = args.boyue_url or DEFAULT_BOYUE_URL
            print(
                f"[paper-task] 已定位 paperID {paper_id}（标注者：{annotator}）",
                flush=True,
            )
            print(f"[paper-task] 论文：{paper}", flush=True)
            print(f"[paper-task] 初始 Web：{web}", flush=True)
            _print_workspace_hint(
                workspace,
                paper_id=paper_id,
                uses_default_workspace=uses_default_workspace,
            )
        else:
            if args.paper is None:
                raise LauncherError("Specify --paperID, or use the legacy --paper path")
            if args.workspace is None:
                raise LauncherError("--workspace is required when --paperID is not used")
            paper = args.paper
            web = args.web
            workspace = args.workspace
            continue_existing = args.continue_existing
            boyue_url = args.boyue_url

        return_code = launch_task(
            workspace,
            paper,
            backend=args.backend,
            codex_bin=args.codex_bin,
            claude_bin=args.claude_bin,
            kimi_bin=args.kimi_bin,
            prepare_only=args.prepare_only,
            continue_existing=continue_existing,
            web_source=web,
            model=args.model,
            claude_model=args.claude_model,
            token_file=args.token_file,
            boyue_url=boyue_url,
            claude_base_url=args.claude_base_url,
            paper_id=paper_id,
            annotator=annotator,
        )
        if paper_id is not None:
            _print_workspace_hint(
                workspace,
                paper_id=paper_id,
                uses_default_workspace=uses_default_workspace,
            )
        return return_code
    except LauncherError as exc:
        print(f"paper-task: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
