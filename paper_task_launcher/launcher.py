from __future__ import annotations

import json
import fcntl
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .claude_recorder import build_claude_settings
from .errors import LauncherError
from .git_history import SnapshotStore, initialize_repository, is_inside_existing_worktree
from .paper import PaperMetadata, import_paper
from .recorder import SessionRecorder
from .util import append_jsonl, atomic_json, run_checked, utc_now

PROMPT_TEMPLATE_VERSION = "paper-learning-web-v5-pdf"
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompt" / "paper_learning_web.txt"


def _load_prompt_template() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise LauncherError(f"Unable to read prompt template: {PROMPT_PATH}: {exc}") from exc


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
    return _load_prompt_template().format(
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


def _load_recording(workspace_value: str) -> tuple[Path, Path, dict]:
    workspace = Path(workspace_value).expanduser().resolve()
    recording_dir = workspace / ".recording"
    manifest_path = recording_dir / "manifest.json"
    if not workspace.is_dir() or not (workspace / ".git").is_dir():
        raise LauncherError(f"Not a paper-task Git workspace: {workspace}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LauncherError(f"Recording manifest not found: {manifest_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LauncherError(f"Unable to read recording manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise LauncherError("Recording manifest must contain a JSON object")
    recorded_workspace = manifest.get("workspace")
    if not isinstance(recorded_workspace, str) or Path(recorded_workspace).resolve() != workspace:
        raise LauncherError("Recording manifest belongs to a different workspace")
    recording_id = manifest.get("recording_id")
    session_id = manifest.get("session_id")
    backend = manifest.get("backend", "codex")
    count = manifest.get("turn_count", 0)
    if not isinstance(recording_id, str) or not recording_id:
        raise LauncherError("Recording manifest has no valid recording ID")
    if not isinstance(session_id, str) or not session_id:
        raise LauncherError("No agent session has been recorded yet; this task cannot be resumed")
    if backend not in {"codex", "claude"}:
        raise LauncherError(f"Unsupported recorded backend: {backend}")
    if not isinstance(count, int) or count < 0:
        raise LauncherError("Recording manifest has an invalid turn count")

    transcript_path = recording_dir / "transcript.jsonl"
    transcript_count = 0
    if transcript_path.exists():
        try:
            for line in transcript_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("record is not an object")
                    transcript_count += 1
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise LauncherError(f"Invalid recording transcript: {exc}") from exc
    if transcript_count != count:
        raise LauncherError(
            f"Recording is inconsistent: manifest has {count} turns but transcript has {transcript_count}"
        )
    previous = manifest.get("latest_snapshot") or manifest.get("baseline_snapshot")
    if not isinstance(previous, dict) or not isinstance(previous.get("commit"), str):
        raise LauncherError("Recording manifest has no valid previous snapshot")
    try:
        run_checked(
            ["git", "cat-file", "-e", f"{previous['commit']}^{{commit}}"],
            cwd=workspace,
        )
    except LauncherError as exc:
        raise LauncherError("The latest recorded Git snapshot is missing") from exc
    return workspace, recording_dir, manifest


@contextmanager
def _recording_lock(recording_dir: Path):
    lock_path = recording_dir / "session.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LauncherError("This task is already open in another launcher process") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def resume_task(
    workspace_value: str,
    *,
    codex_bin: str = "codex",
    claude_bin: str = "claude",
    progress: Callable[[str], None] | None = console_progress,
) -> int:
    workspace, recording_dir, manifest = _load_recording(workspace_value)
    backend = manifest.get("backend", "codex")
    session_id = str(manifest["session_id"])
    agent_bin = codex_bin if backend == "codex" else claude_bin
    agent_name = "Codex" if backend == "codex" else "Claude Code"
    if shutil.which(agent_bin) is None:
        raise LauncherError(f"{agent_name} executable not found: {agent_bin}")

    with _recording_lock(recording_dir):
        # Reload after acquiring the lock so validation and append use the latest state.
        workspace, recording_dir, manifest = _load_recording(workspace_value)
        resume_history = manifest.setdefault("resume_history", [])
        if not isinstance(resume_history, list):
            raise LauncherError("Recording manifest has invalid resume history")
        resume_entry = {
            "index": len(resume_history) + 1,
            "started_at": utc_now(),
            "previous_state": manifest.get("state"),
            "turn_count_before": manifest.get("turn_count", 0),
        }
        resume_history.append(resume_entry)
        manifest["resume_count"] = len(resume_history)
        manifest["state"] = "recording"
        manifest["agent_started_at"] = resume_entry["started_at"]
        atomic_json(recording_dir / "manifest.json", manifest)

        if backend == "codex":
            existing_logs = SessionRecorder.current_logs()
            resume_offsets: dict[Path, int] = {}
            recorded_log = manifest.get("session_log")
            if isinstance(recorded_log, str):
                log_path = Path(recorded_log)
                if log_path.is_file():
                    saved_offset = manifest.get("session_log_offset", 0)
                    if not isinstance(saved_offset, int) or saved_offset < 0:
                        saved_offset = 0
                    resume_offsets[log_path] = min(saved_offset, log_path.stat().st_size)
            recorder = SessionRecorder(
                workspace=workspace,
                recording_dir=recording_dir,
                recording_id=str(manifest["recording_id"]),
                manifest=manifest,
                launch_time=time.time(),
                existing_logs=existing_logs,
                resume_session_id=session_id,
                resume_offsets=resume_offsets,
            )
            recorder.start()
            command = [codex_bin, "resume", "--cd", str(workspace), session_id]
            if progress:
                progress(
                    f"正在恢复 Codex 会话 {session_id}，将从第 {manifest['turn_count'] + 1} 轮继续记录"
                )
            try:
                process = subprocess.Popen(command, cwd=workspace)
                return_code = process.wait()
            except FileNotFoundError as exc:
                raise LauncherError(f"Codex executable not found: {codex_bin}") from exc
            finally:
                recorder.request_stop()
                recorder.join(timeout=10)
            if recorder.is_alive():
                raise LauncherError("Recorder did not stop cleanly")
            if recorder.error:
                manifest["state"] = "recorder_failed"
                manifest["completed_at"] = utc_now()
                atomic_json(recording_dir / "manifest.json", manifest)
                raise LauncherError(f"Session recorder failed: {recorder.error}") from recorder.error
            manifest = recorder.manifest
        else:
            settings_path = recording_dir / "claude-settings.json"
            atomic_json(settings_path, build_claude_settings(workspace, recording_dir))
            errors_path = recording_dir / "hook-errors.jsonl"
            previous_error_size = errors_path.stat().st_size if errors_path.exists() else 0
            command = [claude_bin, "--settings", str(settings_path), "--resume", session_id]
            if progress:
                progress(
                    f"正在恢复 Claude Code 会话 {session_id}，将从第 {manifest['turn_count'] + 1} 轮继续记录"
                )
            try:
                process = subprocess.Popen(command, cwd=workspace)
                return_code = process.wait()
            except FileNotFoundError as exc:
                raise LauncherError(f"Claude Code executable not found: {claude_bin}") from exc
            manifest = json.loads(
                (recording_dir / "manifest.json").read_text(encoding="utf-8")
            )
            current_error_size = errors_path.stat().st_size if errors_path.exists() else 0
            if current_error_size > previous_error_size:
                manifest["state"] = "recorder_failed"
                atomic_json(recording_dir / "manifest.json", manifest)
                raise LauncherError(f"Claude Code recorder failed; see {errors_path}")

        history = manifest.get("resume_history", [])
        if isinstance(history, list) and history:
            history[-1]["completed_at"] = utc_now()
            history[-1]["exit_code"] = return_code
            history[-1]["turn_count_after"] = manifest.get("turn_count", 0)
        manifest["state"] = "completed" if return_code == 0 else f"{backend}_failed"
        manifest["agent_exit_code"] = return_code
        manifest[f"{'codex' if backend == 'codex' else 'claude_code'}_exit_code"] = return_code
        manifest["completed_at"] = utc_now()
        atomic_json(recording_dir / "manifest.json", manifest)
        if progress:
            progress(
                f"{agent_name} 续标会话已结束，现共记录 {manifest.get('turn_count', 0)} 轮"
            )
        return return_code


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
