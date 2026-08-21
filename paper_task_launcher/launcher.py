from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable

from .claude_recorder import build_claude_settings
from .errors import LauncherError
from .git_history import SnapshotStore, initialize_repository, is_inside_existing_worktree
from .paper import PaperMetadata, import_paper
from .recorder import SessionRecorder
from .util import append_jsonl, atomic_json, utc_now

PROMPT_TEMPLATE_VERSION = "paper-learning-web-v4"
PROMPT_TEMPLATE = """你正在一个新建、隔离且独立的 Git 仓库中完成论文学习网站任务。

论文 LaTeX 源码已经导入到：./paper-source/
论文标识：{paper_identity}
论文源码 SHA-256：{paper_sha256}

任务：创建一个前端网页，辅助用户阅读、理解和学习这一整篇论文。网页应对论文信息保持高覆盖率，尽量不遗漏论文中的内容与细节，以满足读者深入学习的需求；并以对用户友好的渐进信息展现方式帮助用户学习。

项目规则：
1. 开始实现前先完整阅读论文源码，网页内容应以论文为依据。
2. ./paper-source/ 仅供读取；不要修改，也不要将其纳入 Git。
3. 项目实现、测试、配置、资源和文档应写在当前仓库的其他位置。
4. 如需使用论文中的图片，将副本放入项目资源目录并注明来源。

请开始执行任务。
"""


def validate_empty_workspace(value: str) -> Path:
    workspace = Path(value).expanduser()
    if is_inside_existing_worktree(workspace):
        raise LauncherError("Workspace must not be inside another Git working tree")
    if workspace.exists():
        if workspace.is_symlink():
            raise LauncherError("Workspace must not be a symbolic link")
        if not workspace.is_dir():
            raise LauncherError("Workspace must be a directory")
        if any(workspace.iterdir()):
            raise LauncherError(f"Workspace is not empty: {workspace}")
    else:
        parent = workspace.parent
        if not parent.exists() or not parent.is_dir():
            raise LauncherError(f"Workspace parent does not exist: {parent}")
        workspace.mkdir()
    workspace = workspace.resolve()
    return workspace


def render_prompt(paper: PaperMetadata) -> str:
    identity = paper.arxiv_id or paper.title or paper.input
    return PROMPT_TEMPLATE.format(
        paper_identity=identity,
        paper_sha256=paper.source_sha256,
    )


class PreparedTask:
    def __init__(self, workspace: Path, recording_id: str, prompt: str, manifest: dict):
        self.workspace = workspace
        self.recording_id = recording_id
        self.prompt = prompt
        self.manifest = manifest


def _cleanup_failed_preparation(workspace: Path) -> None:
    for name in (".recording", "paper-source", ".git", ".gitignore"):
        target = workspace / name
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
        elif target.is_dir():
            shutil.rmtree(target)


def prepare_task(
    workspace_value: str,
    paper_value: str,
    *,
    backend: str = "codex",
    progress: Callable[[str], None] | None = None,
) -> PreparedTask:
    if progress:
        progress("正在检查空工作目录…")
    workspace = validate_empty_workspace(workspace_value)
    recording_id = f"rec-{uuid.uuid4()}"
    recording_dir = workspace / ".recording"
    paper_dir = workspace / "paper-source"
    try:
        recording_dir.mkdir()
        if progress:
            progress("正在准备论文源码…")
        paper = import_paper(paper_value, paper_dir, progress=progress)
        (workspace / ".gitignore").write_text(
            "/paper-source/\n/.recording/\n",
            encoding="utf-8",
        )
        prompt = render_prompt(paper)
        (recording_dir / "initial-prompt.md").write_text(prompt, encoding="utf-8")
        if progress:
            progress("正在初始化独立 Git 仓库…")
        initialize_repository(workspace)
        baseline_store = SnapshotStore(workspace, recording_id)
        baseline = baseline_store.capture("baseline", ref_name="baseline", advance=False)
        manifest = {
            "schema_version": 1,
            "recording_id": recording_id,
            "backend": backend,
            "state": "prepared",
            "created_at": utc_now(),
            "workspace": str(workspace),
            "paper": paper.to_dict(),
            "prompt_template_version": PROMPT_TEMPLATE_VERSION,
            "initial_prompt_path": str(recording_dir / "initial-prompt.md"),
            "session_id": None,
            "turn_count": 0,
            "baseline_snapshot": baseline,
        }
        atomic_json(recording_dir / "manifest.json", manifest)
        append_jsonl(recording_dir / "snapshots.jsonl", baseline)
        if progress:
            progress("任务目录准备完成，基线快照已创建")
        return PreparedTask(workspace, recording_id, prompt, manifest)
    except Exception:
        try:
            _cleanup_failed_preparation(workspace)
            if progress:
                progress("准备失败，已清理任务目录，可直接重试")
        except OSError as cleanup_error:
            if progress:
                progress(f"准备失败，且临时文件清理未完成：{cleanup_error}")
        raise


def console_progress(message: str) -> None:
    print(f"[paper-task] {message}", flush=True)


def launch_task(
    workspace_value: str,
    paper_value: str,
    *,
    backend: str = "codex",
    codex_bin: str = "codex",
    claude_bin: str = "claude",
    prepare_only: bool = False,
    progress: Callable[[str], None] | None = console_progress,
) -> int:
    if backend not in {"codex", "claude"}:
        raise LauncherError(f"Unsupported backend: {backend}")
    agent_bin = codex_bin if backend == "codex" else claude_bin
    agent_name = "Codex" if backend == "codex" else "Claude Code"
    if shutil.which(agent_bin) is None and not prepare_only:
        raise LauncherError(f"{agent_name} executable not found: {agent_bin}")
    task = prepare_task(
        workspace_value,
        paper_value,
        backend=backend,
        progress=progress,
    )
    recording_dir = task.workspace / ".recording"
    if prepare_only:
        task.manifest["state"] = "prepared"
        atomic_json(recording_dir / "manifest.json", task.manifest)
        if progress:
            progress(f"已准备任务：{task.workspace}")
        return 0

    task.manifest["state"] = "recording"
    task.manifest["agent_started_at"] = utc_now()
    if backend == "codex":
        task.manifest["codex_started_at"] = task.manifest["agent_started_at"]
    atomic_json(recording_dir / "manifest.json", task.manifest)
    if backend == "codex":
        existing_logs = SessionRecorder.current_logs()
        launch_time = time.time()
        recorder = SessionRecorder(
            workspace=task.workspace,
            recording_dir=recording_dir,
            recording_id=task.recording_id,
            manifest=task.manifest,
            launch_time=launch_time,
            existing_logs=existing_logs,
        )
        recorder.start()
        command = [codex_bin, "--cd", str(task.workspace), task.prompt]
        if progress:
            progress("正在启动新的 Codex 会话；后续可在会话中自由交互")
        try:
            process = subprocess.Popen(command)
            return_code = process.wait()
        except FileNotFoundError as exc:
            raise LauncherError(f"Codex executable not found: {codex_bin}") from exc
        finally:
            recorder.request_stop()
            recorder.join(timeout=10)
        if recorder.is_alive():
            raise LauncherError("Recorder did not stop cleanly")
        if recorder.error:
            raise LauncherError(f"Session recorder failed: {recorder.error}") from recorder.error
        if recorder.log_path is None:
            raise LauncherError(
                "Codex session log was not found; the task repository was prepared but no turns were recorded"
            )
    else:
        settings_path = recording_dir / "claude-settings.json"
        atomic_json(settings_path, build_claude_settings(task.workspace, recording_dir))
        command = [claude_bin, "--settings", str(settings_path), task.prompt]
        if progress:
            progress("正在启动新的 Claude Code 会话；后续可在会话中自由交互")
        try:
            process = subprocess.Popen(command, cwd=task.workspace)
            return_code = process.wait()
        except FileNotFoundError as exc:
            raise LauncherError(f"Claude Code executable not found: {claude_bin}") from exc
        task.manifest = json.loads(
            (recording_dir / "manifest.json").read_text(encoding="utf-8")
        )
        errors_path = recording_dir / "hook-errors.jsonl"
        if errors_path.exists() and errors_path.stat().st_size:
            task.manifest["state"] = "recorder_failed"
            task.manifest["agent_exit_code"] = return_code
            task.manifest["completed_at"] = utc_now()
            atomic_json(recording_dir / "manifest.json", task.manifest)
            raise LauncherError(f"Claude Code recorder failed; see {errors_path}")
        if not task.manifest.get("session_id"):
            task.manifest["state"] = "recorder_failed"
            task.manifest["agent_exit_code"] = return_code
            task.manifest["completed_at"] = utc_now()
            atomic_json(recording_dir / "manifest.json", task.manifest)
            raise LauncherError(
                "Claude Code hooks did not start; ensure the workspace trust prompt was accepted"
            )
    task.manifest["state"] = "completed" if return_code == 0 else f"{backend}_failed"
    task.manifest["agent_exit_code"] = return_code
    if backend == "codex":
        task.manifest["codex_exit_code"] = return_code
    else:
        task.manifest["claude_code_exit_code"] = return_code
    task.manifest["completed_at"] = utc_now()
    atomic_json(recording_dir / "manifest.json", task.manifest)
    if progress:
        progress(
            f"{agent_name} 会话已结束，共记录 {task.manifest.get('turn_count', 0)} 轮；"
            f"结果位于 {recording_dir}"
        )
    return return_code
