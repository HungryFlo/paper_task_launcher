from __future__ import annotations

import json
import fcntl
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Callable

from .claude_recorder import build_claude_settings
from .claude_provider import (
    boyue_claude_config,
    managed_config,
    select_token as select_claude_token,
    session_environment as claude_session_environment,
    validate_stored_config,
    write_private_json,
)
from .boyue_provider import (
    DEFAULT_BOYUE_URL,
    DEFAULT_TOKEN_FILE,
    normalize_config as normalize_boyue_config,
    record_token_selection,
    validate_stored_config as validate_boyue_config,
)
from .codex_provider import (
    forwarding_relay as codex_forwarding_relay,
    relay_config_override,
    select_token as select_codex_token,
    session_environment as codex_session_environment,
    write_config as write_codex_config,
)
from .errors import LauncherError
from .git_history import (
    SnapshotStore,
    initialize_repository,
    is_inside_existing_worktree,
    is_safe_web_path,
)
from .harness_state import ManagedHarnessHome, recover_harness_home
from .kimi_provider import (
    model_alias as kimi_model_alias,
    select_token as select_kimi_token,
    session_environment as kimi_session_environment,
    write_config as write_kimi_config,
)
from .kimi_recorder import KimiSessionRecorder
from .paper import PaperMetadata, import_paper
from .recorder import SessionRecorder
from .task_locator import is_default_output_workspace
from .util import append_jsonl, atomic_json, run_checked, sha256_file, utc_now

PROMPT_TEMPLATE_VERSION = "paper-learning-web-v5-pdf"
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompt" / "paper_learning_web.txt"
# Backward-compatible patch point retained for callers/tests that referenced the
# original managed-Claude selector directly from this module.
select_token = select_claude_token


def _agent_details(backend: str, codex_bin: str, claude_bin: str, kimi_bin: str):
    if backend == "codex":
        return codex_bin, "Codex", "codex-home"
    if backend == "claude":
        return claude_bin, "Claude Code", "claude-config"
    if backend == "kimi":
        return kimi_bin, "Kimi Code", "kimi-home"
    raise LauncherError(f"Unsupported backend: {backend}")


def _record_harness_sync(
    manifest: dict,
    home: ManagedHarnessHome,
    *,
    backend: str,
) -> None:
    recorded_log = manifest.get("session_log")
    if isinstance(recorded_log, str):
        persisted_log = home.persistent_path_for(Path(recorded_log))
        if persisted_log is not None:
            manifest["session_log"] = str(persisted_log)
    manifest["harness_state"] = {
        "backend": backend,
        "persistent_home": str(home.persistent_home),
        "runtime_policy": "local-mirror",
        "last_synced_at": utc_now(),
    }


def _runtime_session_log(
    manifest: dict,
    home: ManagedHarnessHome,
) -> Path | None:
    recorded_log = manifest.get("session_log")
    if not isinstance(recorded_log, str):
        return None
    runtime_log = home.runtime_path_for(Path(recorded_log))
    if runtime_log is None or not runtime_log.is_file():
        return None
    return runtime_log


def _load_prompt_template() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise LauncherError(f"Unable to read prompt template: {PROMPT_PATH}: {exc}") from exc


def validate_empty_workspace(value: str, *, create: bool = True) -> Path:
    workspace = Path(value).expanduser()
    if is_inside_existing_worktree(workspace) and not is_default_output_workspace(
        workspace
    ):
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
        if create:
            workspace.mkdir()
    workspace = workspace.resolve()
    return workspace


def validate_web_source(value: str, workspace_value: str) -> tuple[Path, str | None]:
    source = Path(value).expanduser()
    if not source.exists():
        raise LauncherError(f"Input web directory does not exist: {source}")
    if source.is_symlink():
        raise LauncherError("Input web directory must not be a symbolic link")
    if not source.is_dir():
        raise LauncherError(f"Input web must be a directory: {source}")
    source = source.resolve()
    workspace = Path(workspace_value).expanduser().resolve()
    if source == workspace:
        raise LauncherError("Input web and workspace must be different directories")
    try:
        workspace.relative_to(source)
    except ValueError:
        pass
    else:
        raise LauncherError("Workspace must not be inside the input web directory")
    try:
        source.relative_to(workspace)
    except ValueError:
        pass
    else:
        raise LauncherError("Input web directory must not be inside the workspace")

    resource_path = source / "resource.json"
    generated_model = None
    if resource_path.is_file():
        try:
            resource = json.loads(resource_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            resource = None
        if isinstance(resource, dict):
            candidate = resource.get("generated_model")
            if isinstance(candidate, str) and candidate.strip():
                generated_model = candidate
    return source, generated_model


def _copy_web_source(source: Path, workspace: Path) -> None:
    for child in source.iterdir():
        relative = Path(child.name)
        if not is_safe_web_path(relative, directory=child.is_dir()):
            continue
        destination = workspace / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(
                child,
                destination,
                symlinks=True,
                ignore=lambda current, names: {
                    name
                    for name in names
                    if not is_safe_web_path(
                        (Path(current) / name).relative_to(source),
                        directory=(Path(current) / name).is_dir(),
                    )
                },
            )
        elif child.is_symlink():
            destination.symlink_to(child.readlink(), target_is_directory=child.is_dir())
        else:
            shutil.copy2(child, destination)


def _clear_workspace(workspace: Path) -> None:
    if not workspace.is_dir():
        return
    for child in workspace.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink(missing_ok=True)
        else:
            shutil.rmtree(child)


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


def validate_continuation_workspace(value: str) -> tuple[Path, bool]:
    """Validate an existing web workspace and report whether it owns a Git repo."""
    workspace = Path(value).expanduser()
    if not workspace.exists():
        raise LauncherError(f"Continuation workspace does not exist: {workspace}")
    if workspace.is_symlink():
        raise LauncherError("Continuation workspace must not be a symbolic link")
    if not workspace.is_dir():
        raise LauncherError("Continuation workspace must be a directory")
    if not any(entry.name != ".git" for entry in workspace.iterdir()):
        raise LauncherError(f"Continuation workspace is empty: {workspace}")
    workspace = workspace.resolve()
    if (workspace / ".recording").exists():
        raise LauncherError(
            "This workspace already has a paper-task recording; use "
            "'paper-task resume --workspace ...' instead of --continue"
        )

    try:
        git_root = Path(
            run_checked(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"])
        ).resolve()
    except LauncherError:
        return workspace, False
    if git_root != workspace:
        raise LauncherError(
            "Continuation workspace must not be nested inside another Git working tree"
        )
    if not (workspace / ".git").is_dir():
        raise LauncherError(
            "Continuation workspace must be a normal Git repository, not a linked worktree"
        )
    return workspace, True


def _exclude_launcher_data_from_repository(workspace: Path) -> None:
    exclude_path = workspace / ".git" / "info" / "exclude"
    try:
        existing = exclude_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    except OSError as exc:
        raise LauncherError(f"Unable to read Git exclude file: {exclude_path}: {exc}") from exc
    required_patterns = ("/.recording/", "/paper-source/", "/out_data/")
    existing_patterns = {line.strip() for line in existing.splitlines()}
    missing_patterns = [
        pattern for pattern in required_patterns if pattern not in existing_patterns
    ]
    if not missing_patterns:
        return
    separator = "" if not existing or existing.endswith("\n") else "\n"
    addition = "".join(pattern + "\n" for pattern in missing_patterns)
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.write_text(
            existing + separator + addition,
            encoding="utf-8",
        )
    except OSError as exc:
        raise LauncherError(f"Unable to update Git exclude file: {exclude_path}: {exc}") from exc


def prepare_continuation_task(
    workspace_value: str,
    paper_value: str,
    *,
    backend: str = "codex",
    progress: Callable[[str], None] | None = None,
) -> PreparedTask:
    if progress:
        progress("正在检查已有 Web 工作目录…")
    workspace, had_git_repository = validate_continuation_workspace(workspace_value)
    recording_id = f"rec-{uuid.uuid4()}"
    recording_dir = workspace / ".recording"
    paper_dir = workspace / "paper-source"
    if paper_dir.exists():
        raise LauncherError(
            "Continuation workspace already contains paper-source; move or remove it "
            "before starting a new recording"
        )
    created_git_repository = False
    try:
        if not had_git_repository:
            if progress:
                progress("正在为已有 Web 初始化独立 Git 仓库…")
            run_checked(["git", "init", "-b", "main", str(workspace)])
            created_git_repository = True
        _exclude_launcher_data_from_repository(workspace)
        recording_dir.mkdir()
        if progress:
            progress("正在导入论文源文件…")
        paper = import_paper(paper_value, paper_dir, progress=progress)

        baseline_store = SnapshotStore(workspace, recording_id)
        baseline = baseline_store.capture("baseline", ref_name="baseline", advance=False)
        if created_git_repository:
            # Make the imported existing web the clean initial state of the
            # repository we just created. This affects only Git metadata and
            # lets the coding agent inspect subsequent edits with git diff.
            run_checked(
                ["git", "update-ref", "refs/heads/main", baseline["commit"]],
                cwd=workspace,
            )
            run_checked(["git", "read-tree", baseline["commit"]], cwd=workspace)
        manifest = {
            "schema_version": 1,
            "recording_id": recording_id,
            "backend": backend,
            "task_mode": "continue",
            "state": "prepared",
            "created_at": utc_now(),
            "workspace": str(workspace),
            "paper": paper.to_dict(),
            "prompt_template_version": None,
            "initial_prompt_path": None,
            "session_id": None,
            "turn_count": 0,
            "baseline_snapshot": baseline,
            "continued_workspace_had_git": had_git_repository,
        }
        atomic_json(recording_dir / "manifest.json", manifest)
        append_jsonl(recording_dir / "snapshots.jsonl", baseline)
        if progress:
            progress("已将现有 Web 记录为基线；未生成或发送论文建站 prompt")
        return PreparedTask(workspace, recording_id, "", manifest)
    except Exception:
        if recording_dir.is_dir():
            shutil.rmtree(recording_dir)
        if paper_dir.is_dir():
            shutil.rmtree(paper_dir)
        if created_git_repository and (workspace / ".git").is_dir():
            shutil.rmtree(workspace / ".git")
        raise


def prepare_web_task(
    workspace_value: str,
    paper_value: str,
    web_source: Path,
    generated_model: str | None,
    modification_model: str,
    *,
    backend: str,
    progress: Callable[[str], None] | None = None,
) -> PreparedTask:
    if progress:
        progress("正在创建独立 workspace 并复制已有 Web…")
    workspace = validate_empty_workspace(workspace_value)
    recording_id = f"rec-{uuid.uuid4()}"
    recording_dir = workspace / ".recording"
    paper_dir = workspace / "paper-source"
    try:
        _copy_web_source(web_source, workspace)
        recording_dir.mkdir()
        if progress:
            progress("正在导入论文源文件…")
        paper = import_paper(paper_value, paper_dir, progress=progress)
        run_checked(["git", "init", "-b", "main", str(workspace)])
        _exclude_launcher_data_from_repository(workspace)
        baseline_store = SnapshotStore(
            workspace, recording_id, snapshot_policy="web"
        )
        baseline = baseline_store.capture(
            "baseline", ref_name="baseline", advance=False
        )
        run_checked(
            ["git", "update-ref", "refs/heads/main", baseline["commit"]],
            cwd=workspace,
        )
        run_checked(["git", "read-tree", baseline["commit"]], cwd=workspace)
        manifest = {
            "schema_version": 2,
            "recording_id": recording_id,
            "backend": backend,
            "task_mode": "web_copy",
            "snapshot_policy": "web",
            "state": "prepared",
            "created_at": utc_now(),
            "workspace": str(workspace),
            "paper": paper.to_dict(),
            "prompt_template_version": None,
            "initial_prompt_path": None,
            "session_id": None,
            "turn_count": 0,
            "baseline_snapshot": baseline,
            "generated_model": generated_model,
            "modification_model": modification_model,
            "input_web": {
                "source_path": str(web_source),
                "resource_sha256": (
                    sha256_file(web_source / "resource.json")
                    if (web_source / "resource.json").is_file()
                    else None
                ),
            },
        }
        atomic_json(recording_dir / "manifest.json", manifest)
        append_jsonl(recording_dir / "snapshots.jsonl", baseline)
        if progress:
            progress(
                f"已复制现有 Web 并记录为基线；修改模型为 {modification_model}，"
                "未生成或发送论文建站 prompt"
            )
        return PreparedTask(workspace, recording_id, "", manifest)
    except Exception:
        try:
            _clear_workspace(workspace)
            if progress:
                progress("准备失败，已清理新 workspace，可直接重试")
        except OSError as cleanup_error:
            if progress:
                progress(f"准备失败，且临时文件清理未完成：{cleanup_error}")
        raise


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
            "/paper-source/\n/.recording/\n/out_data/\n",
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
    if backend not in {"codex", "claude", "kimi"}:
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
    kimi_bin: str = "kimi",
    progress: Callable[[str], None] | None = console_progress,
) -> int:
    workspace, recording_dir, manifest = _load_recording(workspace_value)
    backend = manifest.get("backend", "codex")
    session_id = str(manifest["session_id"])
    agent_bin, agent_name, config_dir_name = _agent_details(
        backend, codex_bin, claude_bin, kimi_bin
    )
    if shutil.which(agent_bin) is None:
        raise LauncherError(f"{agent_name} executable not found: {agent_bin}")

    with _recording_lock(recording_dir):
        # Reload after acquiring the lock so validation and append use the latest state.
        workspace, recording_dir, manifest = _load_recording(workspace_value)
        selected_token = None
        boyue_config = validate_boyue_config(
            manifest.get("boyue_provider"), backend=backend
        )
        legacy_claude_config = None
        if boyue_config is not None:
            if backend == "claude":
                selected_token = select_claude_token(
                    boyue_claude_config(boyue_config), claude_bin, progress=progress
                )
            elif backend == "codex":
                selected_token = select_codex_token(
                    boyue_config, codex_bin, progress=progress
                )
            else:
                selected_token = select_kimi_token(
                    boyue_config, kimi_bin, progress=progress
                )
            record_token_selection(boyue_config, selected_token)
        elif backend == "claude":
            legacy_claude_config = validate_stored_config(
                manifest.get("claude_provider")
            )
            if legacy_claude_config is not None:
                selected_token = select_claude_token(
                    legacy_claude_config, claude_bin, progress=progress
                )
                record_token_selection(legacy_claude_config, selected_token)
        if boyue_config is not None:
            config_dir = recording_dir / config_dir_name
            recover_harness_home(config_dir)
            recorded_log = manifest.get("session_log")
            valid_local_log = False
            if isinstance(recorded_log, str):
                log_path = Path(recorded_log)
                if log_path.is_file():
                    try:
                        log_path.resolve().relative_to(config_dir.resolve())
                    except ValueError:
                        pass
                    else:
                        valid_local_log = True
            if not valid_local_log:
                raise LauncherError(
                    f"Recorded {agent_name} session state is missing from the task-local "
                    "configuration directory; this task cannot be resumed"
                )
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
            process_env = None
            sessions_root = None
            command = [codex_bin, "resume", "--cd", str(workspace)]
            harness_home = None
            if boyue_config is not None:
                persistent_home = recording_dir / "codex-home"
                recover_harness_home(persistent_home)
                write_codex_config(
                    persistent_home, boyue_config, workspace=workspace
                )
                home_context = ManagedHarnessHome(
                    persistent_home,
                    str(manifest["recording_id"]),
                    require_mmap=True,
                )
            else:
                home_context = nullcontext(None)
            with home_context as harness_home:
                if boyue_config is not None:
                    assert harness_home is not None
                    config_dir = harness_home.runtime_home
                    write_codex_config(config_dir, boyue_config, workspace=workspace)
                    assert selected_token is not None
                    process_env = codex_session_environment(
                        boyue_config, selected_token.value, config_dir
                    )
                    sessions_root = config_dir / "sessions"
                    command.extend(["--model", str(boyue_config["model"])])
                command.append(session_id)
                existing_logs = SessionRecorder.current_logs(sessions_root)
                resume_offsets: dict[Path, int] = {}
                if harness_home is not None:
                    log_path = _runtime_session_log(manifest, harness_home)
                else:
                    recorded_log = manifest.get("session_log")
                    log_path = (
                        Path(recorded_log)
                        if isinstance(recorded_log, str)
                        and Path(recorded_log).is_file()
                        else None
                    )
                if log_path is not None:
                    saved_offset = manifest.get("session_log_offset", 0)
                    if not isinstance(saved_offset, int) or saved_offset < 0:
                        saved_offset = 0
                    resume_offsets[log_path] = min(
                        saved_offset, log_path.stat().st_size
                    )
                recorder = SessionRecorder(
                    workspace=workspace,
                    recording_dir=recording_dir,
                    recording_id=str(manifest["recording_id"]),
                    manifest=manifest,
                    launch_time=time.time(),
                    existing_logs=existing_logs,
                    resume_session_id=session_id,
                    resume_offsets=resume_offsets,
                    sessions_root=sessions_root,
                )
                recorder.start()
                if progress:
                    progress(
                        f"正在恢复 Codex 会话 {session_id}，将从第 {manifest['turn_count'] + 1} 轮继续记录"
                    )
                try:
                    if boyue_config is not None:
                        with codex_forwarding_relay(
                            str(boyue_config["api_base_url"])
                        ) as relay:
                            relay_command = [
                                codex_bin,
                                "--config",
                                relay_config_override(relay.base_url),
                                *command[1:],
                            ]
                            process = subprocess.Popen(
                                relay_command, cwd=workspace, env=process_env
                            )
                            return_code = process.wait()
                    else:
                        process = subprocess.Popen(
                            command, cwd=workspace, env=process_env
                        )
                        return_code = process.wait()
                except FileNotFoundError as exc:
                    raise LauncherError(
                        f"Codex executable not found: {codex_bin}"
                    ) from exc
                finally:
                    recorder.request_stop()
                    recorder.join(timeout=10)
                if recorder.is_alive():
                    raise LauncherError("Recorder did not stop cleanly")
                if recorder.error:
                    manifest["state"] = "recorder_failed"
                    manifest["completed_at"] = utc_now()
                    atomic_json(recording_dir / "manifest.json", manifest)
                    raise LauncherError(
                        f"Session recorder failed: {recorder.error}"
                    ) from recorder.error
                manifest = recorder.manifest
            if harness_home is not None:
                _record_harness_sync(manifest, harness_home, backend=backend)
        elif backend == "claude":
            active_claude_config = (
                boyue_claude_config(boyue_config)
                if boyue_config is not None
                else legacy_claude_config
            )
            harness_home = None
            if active_claude_config is not None:
                persistent_home = recording_dir / "claude-config"
                recover_harness_home(persistent_home)
                settings_path = persistent_home / "settings.json"
                write_private_json(
                    settings_path, build_claude_settings(workspace, recording_dir)
                )
                home_context = ManagedHarnessHome(
                    persistent_home,
                    str(manifest["recording_id"]),
                )
            else:
                home_context = nullcontext(None)
            with home_context as harness_home:
                if active_claude_config is not None:
                    assert harness_home is not None
                    config_dir = harness_home.runtime_home
                    settings_path = config_dir / "settings.json"
                    write_private_json(
                        settings_path,
                        build_claude_settings(workspace, recording_dir),
                    )
                    assert selected_token is not None
                    process_env = claude_session_environment(
                        active_claude_config,
                        selected_token.value,
                        config_dir,
                    )
                    command = [
                        claude_bin,
                        "--settings",
                        str(settings_path),
                        "--model",
                        str(active_claude_config["model"]),
                        "--resume",
                        session_id,
                    ]
                else:
                    settings_path = recording_dir / "claude-settings.json"
                    atomic_json(
                        settings_path,
                        build_claude_settings(workspace, recording_dir),
                    )
                    process_env = None
                    command = [
                        claude_bin,
                        "--settings",
                        str(settings_path),
                        "--resume",
                        session_id,
                    ]
                errors_path = recording_dir / "hook-errors.jsonl"
                previous_error_size = (
                    errors_path.stat().st_size if errors_path.exists() else 0
                )
                if progress:
                    progress(
                        f"正在恢复 Claude Code 会话 {session_id}，将从第 {manifest['turn_count'] + 1} 轮继续记录"
                    )
                try:
                    process = subprocess.Popen(
                        command, cwd=workspace, env=process_env
                    )
                    return_code = process.wait()
                except FileNotFoundError as exc:
                    raise LauncherError(
                        f"Claude Code executable not found: {claude_bin}"
                    ) from exc
                manifest = json.loads(
                    (recording_dir / "manifest.json").read_text(
                        encoding="utf-8"
                    )
                )
                current_error_size = (
                    errors_path.stat().st_size if errors_path.exists() else 0
                )
                if current_error_size > previous_error_size:
                    manifest["state"] = "recorder_failed"
                    atomic_json(recording_dir / "manifest.json", manifest)
                    raise LauncherError(
                        f"Claude Code recorder failed; see {errors_path}"
                    )
            if harness_home is not None:
                _record_harness_sync(manifest, harness_home, backend=backend)
        else:
            if boyue_config is None or selected_token is None:
                raise LauncherError("Kimi recording has no valid Boyue configuration")
            persistent_home = recording_dir / "kimi-home"
            recover_harness_home(persistent_home)
            write_kimi_config(persistent_home, boyue_config)
            with ManagedHarnessHome(
                persistent_home,
                str(manifest["recording_id"]),
            ) as harness_home:
                config_dir = harness_home.runtime_home
                write_kimi_config(config_dir, boyue_config)
                process_env = kimi_session_environment(
                    boyue_config, selected_token.value, config_dir
                )
                saved_offset = manifest.get("session_log_offset", 0)
                if not isinstance(saved_offset, int) or saved_offset < 0:
                    saved_offset = 0
                recorder = KimiSessionRecorder(
                    workspace=workspace,
                    recording_dir=recording_dir,
                    recording_id=str(manifest["recording_id"]),
                    manifest=manifest,
                    config_dir=config_dir,
                    launch_time=time.time(),
                    resume_session_id=session_id,
                    resume_offset=saved_offset,
                )
                recorder.start()
                command = [
                    kimi_bin,
                    "--session",
                    session_id,
                    "--model",
                    kimi_model_alias(boyue_config),
                ]
                if progress:
                    progress(
                        f"正在恢复 Kimi Code 会话 {session_id}，"
                        f"将从第 {manifest['turn_count'] + 1} 轮继续记录"
                    )
                try:
                    process = subprocess.Popen(
                        command, cwd=workspace, env=process_env
                    )
                    return_code = process.wait()
                except FileNotFoundError as exc:
                    raise LauncherError(
                        f"Kimi Code executable not found: {kimi_bin}"
                    ) from exc
                finally:
                    recorder.request_stop()
                    recorder.join(timeout=10)
                if recorder.is_alive():
                    raise LauncherError("Kimi recorder did not stop cleanly")
                if recorder.error:
                    manifest["state"] = "recorder_failed"
                    atomic_json(recording_dir / "manifest.json", manifest)
                    raise LauncherError(
                        f"Kimi session recorder failed: {recorder.error}"
                    ) from recorder.error
                manifest = recorder.manifest
            _record_harness_sync(manifest, harness_home, backend=backend)

        history = manifest.get("resume_history", [])
        if isinstance(history, list) and history:
            history[-1]["completed_at"] = utc_now()
            history[-1]["exit_code"] = return_code
            history[-1]["turn_count_after"] = manifest.get("turn_count", 0)
        manifest["state"] = "completed" if return_code == 0 else f"{backend}_failed"
        manifest["agent_exit_code"] = return_code
        exit_key = {
            "codex": "codex_exit_code",
            "claude": "claude_code_exit_code",
            "kimi": "kimi_code_exit_code",
        }[backend]
        manifest[exit_key] = return_code
        manifest["completed_at"] = utc_now()
        atomic_json(recording_dir / "manifest.json", manifest)
        if progress:
            progress(
                f"{agent_name} 续标会话已结束，现共记录 {manifest.get('turn_count', 0)} 轮"
            )
        return return_code


def launch_task(
    workspace_value: str,
    paper_value: str | None = None,
    *,
    backend: str = "codex",
    codex_bin: str = "codex",
    claude_bin: str = "claude",
    kimi_bin: str = "kimi",
    prepare_only: bool = False,
    continue_existing: bool = False,
    web_source: str | None = None,
    model: str | None = None,
    claude_model: str | None = None,
    token_file: str | None = None,
    boyue_url: str | None = None,
    claude_base_url: str | None = None,
    paper_id: str | None = None,
    annotator: str | None = None,
    progress: Callable[[str], None] | None = console_progress,
) -> int:
    if backend not in {"codex", "claude", "kimi"}:
        raise LauncherError(f"Unsupported backend: {backend}")
    if boyue_url is not None and claude_base_url is not None:
        raise LauncherError("Use only --boyue-url; do not combine it with --claude-base-url")
    effective_url = boyue_url if boyue_url is not None else claude_base_url
    if paper_value is None:
        raise LauncherError("--paper is required")
    validated_web: tuple[Path, str | None] | None = None
    boyue_config = None
    legacy_claude_config = None
    if web_source is not None:
        if claude_model is not None:
            raise LauncherError(
                "--claude-model cannot be used with --web; use the backend-neutral "
                "--model option instead"
            )
        if model is None or not model.strip():
            raise LauncherError("--web requires a non-empty --model")
        validated_web = validate_web_source(web_source, workspace_value)
        validate_empty_workspace(workspace_value, create=False)
        if not continue_existing and progress:
            progress(
                "提示：--web 单独使用仍受兼容支持；推荐以后同时传入 --continue --web"
            )
        effective_token_file = token_file or str(DEFAULT_TOKEN_FILE)
        effective_url = boyue_url or claude_base_url or DEFAULT_BOYUE_URL
        boyue_config = normalize_boyue_config(
            backend=backend,
            model=model,
            token_file=effective_token_file,
            base_url=str(effective_url),
        )
        try:
            Path(boyue_config["token_file"]).relative_to(validated_web[0])
        except ValueError:
            pass
        else:
            raise LauncherError(
                "--token-file must be outside the input web directory so credentials "
                "cannot enter Web snapshots"
            )
    else:
        if backend == "kimi":
            if claude_model is not None:
                raise LauncherError("--claude-model cannot be used with --backend kimi")
            if model is None or not model.strip():
                raise LauncherError("--backend kimi requires a non-empty --model")
            boyue_config = normalize_boyue_config(
                backend=backend,
                model=model,
                token_file=token_file or str(DEFAULT_TOKEN_FILE),
                base_url=effective_url or DEFAULT_BOYUE_URL,
            )
        else:
            if model is not None:
                raise LauncherError("--model is only supported with --web or --backend kimi")
            if (claude_model is not None or token_file is not None) and effective_url is None:
                effective_url = DEFAULT_BOYUE_URL
            legacy_claude_config = managed_config(
                backend=backend,
                model=claude_model,
                token_file=token_file,
                base_url=effective_url,
            )
    agent_bin, agent_name, config_dir_name = _agent_details(
        backend, codex_bin, claude_bin, kimi_bin
    )
    if shutil.which(agent_bin) is None and not prepare_only:
        raise LauncherError(f"{agent_name} executable not found: {agent_bin}")
    selected_token = None
    if not prepare_only and boyue_config is not None:
        if backend == "claude":
            selected_token = select_claude_token(
                boyue_claude_config(boyue_config), claude_bin, progress=progress
            )
        elif backend == "codex":
            selected_token = select_codex_token(
                boyue_config, codex_bin, progress=progress
            )
        else:
            selected_token = select_kimi_token(
                boyue_config, kimi_bin, progress=progress
            )
        record_token_selection(boyue_config, selected_token)
    elif not prepare_only and legacy_claude_config is not None:
        selected_token = select_claude_token(
            legacy_claude_config, claude_bin, progress=progress
        )
        record_token_selection(legacy_claude_config, selected_token)

    if validated_web is not None:
        task = prepare_web_task(
            workspace_value,
            paper_value,
            validated_web[0],
            validated_web[1],
            str(model),
            backend=backend,
            progress=progress,
        )
    elif continue_existing:
        task = prepare_continuation_task(
            workspace_value,
            paper_value,
            backend=backend,
            progress=progress,
        )
    else:
        task = prepare_task(
            workspace_value,
            paper_value,
            backend=backend,
            progress=progress,
        )
    recording_dir = task.workspace / ".recording"
    if paper_id is not None:
        task.manifest["paper_id"] = paper_id
        if annotator is not None:
            task.manifest["annotator"] = annotator
        atomic_json(recording_dir / "manifest.json", task.manifest)
    if boyue_config is not None:
        task.manifest["boyue_provider"] = boyue_config
        task.manifest["agent_config_dir"] = str(
            recording_dir / config_dir_name
        )
        atomic_json(recording_dir / "manifest.json", task.manifest)
    elif legacy_claude_config is not None:
        task.manifest["claude_provider"] = legacy_claude_config
        atomic_json(recording_dir / "manifest.json", task.manifest)
    if prepare_only:
        if boyue_config is not None:
            if backend == "codex":
                write_codex_config(
                    recording_dir / "codex-home",
                    boyue_config,
                    workspace=task.workspace,
                )
            elif backend == "claude":
                write_private_json(
                    recording_dir / "claude-config" / "settings.json",
                    build_claude_settings(task.workspace, recording_dir),
                )
            else:
                write_kimi_config(recording_dir / "kimi-home", boyue_config)
        task.manifest["state"] = "prepared"
        atomic_json(recording_dir / "manifest.json", task.manifest)
        if progress:
            progress(f"已准备任务：{task.workspace}")
        return 0

    task.manifest["state"] = "recording"
    task.manifest["agent_started_at"] = utc_now()
    if backend == "codex":
        task.manifest["codex_started_at"] = task.manifest["agent_started_at"]
    elif backend == "kimi":
        task.manifest["kimi_started_at"] = task.manifest["agent_started_at"]
    atomic_json(recording_dir / "manifest.json", task.manifest)
    if backend == "codex":
        process_env = None
        sessions_root = None
        command = [codex_bin, "--cd", str(task.workspace)]
        harness_home = None
        if boyue_config is not None:
            persistent_home = recording_dir / "codex-home"
            recover_harness_home(persistent_home)
            write_codex_config(
                persistent_home, boyue_config, workspace=task.workspace
            )
            home_context = ManagedHarnessHome(
                persistent_home,
                task.recording_id,
                require_mmap=True,
            )
        else:
            home_context = nullcontext(None)
        with home_context as harness_home:
            if boyue_config is not None:
                assert harness_home is not None
                config_dir = harness_home.runtime_home
                write_codex_config(
                    config_dir, boyue_config, workspace=task.workspace
                )
                assert selected_token is not None
                process_env = codex_session_environment(
                    boyue_config, selected_token.value, config_dir
                )
                sessions_root = config_dir / "sessions"
                command.extend(["--model", str(boyue_config["model"])])
            existing_logs = SessionRecorder.current_logs(sessions_root)
            launch_time = time.time()
            recorder = SessionRecorder(
                workspace=task.workspace,
                recording_dir=recording_dir,
                recording_id=task.recording_id,
                manifest=task.manifest,
                launch_time=launch_time,
                existing_logs=existing_logs,
                sessions_root=sessions_root,
            )
            recorder.start()
            if not continue_existing and validated_web is None:
                command.append(task.prompt)
            if progress:
                if continue_existing or validated_web is not None:
                    progress("正在启动 Codex 修改会话；请在会话中输入修改要求")
                else:
                    progress("正在启动新的 Codex 会话；后续可在会话中自由交互")
            try:
                if boyue_config is not None:
                    with codex_forwarding_relay(
                        str(boyue_config["api_base_url"])
                    ) as relay:
                        relay_command = [
                            codex_bin,
                            "--config",
                            relay_config_override(relay.base_url),
                            *command[1:],
                        ]
                        process = subprocess.Popen(
                            relay_command,
                            cwd=task.workspace,
                            env=process_env,
                        )
                        return_code = process.wait()
                else:
                    process = subprocess.Popen(
                        command, cwd=task.workspace, env=process_env
                    )
                    return_code = process.wait()
            except FileNotFoundError as exc:
                raise LauncherError(
                    f"Codex executable not found: {codex_bin}"
                ) from exc
            finally:
                recorder.request_stop()
                recorder.join(timeout=10)
            if recorder.is_alive():
                raise LauncherError("Recorder did not stop cleanly")
            if recorder.error:
                raise LauncherError(
                    f"Session recorder failed: {recorder.error}"
                ) from recorder.error
            if recorder.log_path is None:
                if return_code != 0:
                    raise LauncherError(
                        f"Codex exited with status {return_code} before creating "
                        "a session log; see the Codex error above"
                    )
                raise LauncherError(
                    "Codex session log was not found; the task repository was prepared but no turns were recorded"
                )
            task.manifest = recorder.manifest
        if harness_home is not None:
            _record_harness_sync(task.manifest, harness_home, backend=backend)
    elif backend == "claude":
        active_claude_config = (
            boyue_claude_config(boyue_config)
            if boyue_config is not None
            else legacy_claude_config
        )
        harness_home = None
        if active_claude_config is not None:
            persistent_home = recording_dir / "claude-config"
            recover_harness_home(persistent_home)
            settings_path = persistent_home / "settings.json"
            write_private_json(
                settings_path, build_claude_settings(task.workspace, recording_dir)
            )
            home_context = ManagedHarnessHome(
                persistent_home,
                task.recording_id,
            )
        else:
            home_context = nullcontext(None)
        with home_context as harness_home:
            if active_claude_config is not None:
                assert harness_home is not None
                config_dir = harness_home.runtime_home
                settings_path = config_dir / "settings.json"
                write_private_json(
                    settings_path,
                    build_claude_settings(task.workspace, recording_dir),
                )
                assert selected_token is not None
                process_env = claude_session_environment(
                    active_claude_config,
                    selected_token.value,
                    config_dir,
                )
                command = [
                    claude_bin,
                    "--settings",
                    str(settings_path),
                    "--model",
                    str(active_claude_config["model"]),
                ]
            else:
                settings_path = recording_dir / "claude-settings.json"
                atomic_json(
                    settings_path,
                    build_claude_settings(task.workspace, recording_dir),
                )
                process_env = None
                command = [claude_bin, "--settings", str(settings_path)]
            if not continue_existing and validated_web is None:
                command.append(task.prompt)
            if progress:
                if continue_existing or validated_web is not None:
                    progress(
                        "正在启动 Claude Code 修改会话；请在会话中输入修改要求"
                    )
                else:
                    progress(
                        "正在启动新的 Claude Code 会话；后续可在会话中自由交互"
                    )
            try:
                process = subprocess.Popen(
                    command, cwd=task.workspace, env=process_env
                )
                return_code = process.wait()
            except FileNotFoundError as exc:
                raise LauncherError(
                    f"Claude Code executable not found: {claude_bin}"
                ) from exc
            task.manifest = json.loads(
                (recording_dir / "manifest.json").read_text(encoding="utf-8")
            )
            errors_path = recording_dir / "hook-errors.jsonl"
            if errors_path.exists() and errors_path.stat().st_size:
                task.manifest["state"] = "recorder_failed"
                task.manifest["agent_exit_code"] = return_code
                task.manifest["completed_at"] = utc_now()
                atomic_json(recording_dir / "manifest.json", task.manifest)
                raise LauncherError(
                    f"Claude Code recorder failed; see {errors_path}"
                )
            if not task.manifest.get("session_id"):
                task.manifest["state"] = "recorder_failed"
                task.manifest["agent_exit_code"] = return_code
                task.manifest["completed_at"] = utc_now()
                atomic_json(recording_dir / "manifest.json", task.manifest)
                raise LauncherError(
                    "Claude Code hooks did not start; ensure the workspace trust prompt was accepted"
                )
        if harness_home is not None:
            _record_harness_sync(task.manifest, harness_home, backend=backend)
    else:
        if boyue_config is None or selected_token is None:
            raise LauncherError("Kimi backend requires a valid Boyue token configuration")
        persistent_home = recording_dir / "kimi-home"
        recover_harness_home(persistent_home)
        write_kimi_config(persistent_home, boyue_config)
        with ManagedHarnessHome(
            persistent_home,
            task.recording_id,
        ) as harness_home:
            config_dir = harness_home.runtime_home
            write_kimi_config(config_dir, boyue_config)
            process_env = kimi_session_environment(
                boyue_config, selected_token.value, config_dir
            )
            recorder = KimiSessionRecorder(
                workspace=task.workspace,
                recording_dir=recording_dir,
                recording_id=task.recording_id,
                manifest=task.manifest,
                config_dir=config_dir,
                launch_time=time.time(),
            )
            recorder.start()
            command = [kimi_bin, "--model", kimi_model_alias(boyue_config)]
            if not continue_existing and validated_web is None:
                command.extend(["--prompt", task.prompt])
            if progress:
                if continue_existing or validated_web is not None:
                    progress(
                        "正在启动 Kimi Code 修改会话；请在会话中输入修改要求"
                    )
                else:
                    progress("正在启动 Kimi Code 生成任务")
            try:
                process = subprocess.Popen(
                    command, cwd=task.workspace, env=process_env
                )
                return_code = process.wait()
            except FileNotFoundError as exc:
                raise LauncherError(
                    f"Kimi Code executable not found: {kimi_bin}"
                ) from exc
            finally:
                recorder.request_stop()
                recorder.join(timeout=10)
            if recorder.is_alive():
                raise LauncherError("Kimi recorder did not stop cleanly")
            if recorder.error:
                raise LauncherError(
                    f"Kimi session recorder failed: {recorder.error}"
                ) from recorder.error
            if recorder.log_path is None or recorder.session_id is None:
                raise LauncherError(
                    "Kimi session log was not found; submit at least one prompt before exiting"
                )
            task.manifest = recorder.manifest
        _record_harness_sync(task.manifest, harness_home, backend=backend)
    task.manifest["state"] = "completed" if return_code == 0 else f"{backend}_failed"
    task.manifest["agent_exit_code"] = return_code
    exit_key = {
        "codex": "codex_exit_code",
        "claude": "claude_code_exit_code",
        "kimi": "kimi_code_exit_code",
    }[backend]
    task.manifest[exit_key] = return_code
    task.manifest["completed_at"] = utc_now()
    atomic_json(recording_dir / "manifest.json", task.manifest)
    if progress:
        progress(
            f"{agent_name} 会话已结束，共记录 {task.manifest.get('turn_count', 0)} 轮；"
            f"结果位于 {recording_dir}"
        )
    return return_code
