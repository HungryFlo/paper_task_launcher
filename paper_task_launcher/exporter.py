from __future__ import annotations

import json
import os
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Callable

from .errors import LauncherError
from .util import atomic_json, run_checked, utc_now


EXPORT_SCHEMA = "paper-task-dataset-v1"


def _export_manifest(manifest: dict) -> dict:
    """Return export-safe recording metadata without local credential references."""
    result = deepcopy(manifest)
    provider = result.get("claude_provider")
    if isinstance(provider, dict):
        provider.pop("token_file", None)
        provider.pop("last_selected_token_label", None)
    provider = result.get("boyue_provider")
    if isinstance(provider, dict):
        provider.pop("token_file", None)
        provider.pop("last_selected_token_label", None)
    input_web = result.get("input_web")
    if isinstance(input_web, dict):
        input_web.pop("source_path", None)
    for key in ("workspace", "session_log", "initial_prompt_path", "agent_config_dir"):
        result.pop(key, None)
    return result


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LauncherError(f"Recording file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LauncherError(f"Unable to read recording file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LauncherError(f"Recording file must contain a JSON object: {path}")
    return value


def _load_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise LauncherError(f"Recording file not found: {path}") from exc
    except OSError as exc:
        raise LauncherError(f"Unable to read recording file {path}: {exc}") from exc
    records: list[dict] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LauncherError(f"Invalid JSON at {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise LauncherError(f"Expected a JSON object at {path}:{line_number}")
        records.append(value)
    return records


def _validate_output(workspace: Path, output_value: str | None) -> Path:
    default_output = (workspace / "out_data").resolve()
    output = Path(output_value).expanduser() if output_value else default_output
    if output.exists():
        if output.is_symlink() or not output.is_dir():
            raise LauncherError(f"Export output must be a directory: {output}")
        if any(output.iterdir()):
            raise LauncherError(f"Export output is not empty: {output}")
    elif not output.parent.exists() or not output.parent.is_dir():
        raise LauncherError(f"Export output parent does not exist: {output.parent}")
    output = output.resolve()
    try:
        output.relative_to(workspace)
    except ValueError:
        pass
    else:
        if output != default_output:
            raise LauncherError(
                "Export output inside the task workspace must be its out_data directory"
            )
    return output


def _export_commit(workspace: Path, commit: str, destination: Path) -> None:
    run_checked(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=workspace)
    destination.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="paper-task-export-index-") as temporary:
        index_path = Path(temporary) / "index"
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(index_path)
        run_checked(["git", "read-tree", commit], cwd=workspace, env=env)
        run_checked(
            [
                "git",
                "checkout-index",
                "--all",
                f"--prefix={destination}{os.sep}",
            ],
            cwd=workspace,
            env=env,
        )


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _validate_required_turn_content(turns: list[dict]) -> None:
    if not turns:
        raise LauncherError(
            "Recording contains no completed turns; export was aborted to avoid creating "
            "an incomplete dataset"
        )

    incomplete: list[str] = []
    for position, turn in enumerate(turns, 1):
        index = turn.get("turn_index")
        label = f"turn {index}" if isinstance(index, int) else f"record {position}"
        missing: list[str] = []
        user_input = turn.get("user_input")
        if not isinstance(user_input, str) or not user_input.strip():
            missing.append("user_input")
        # A completed Codex task may legitimately contain
        # last_agent_message=null (for example, an interrupted turn). Keep
        # that as an empty string instead of inventing a response. The field
        # must still exist and have the expected type so parser/data loss is
        # not silently exported.
        if "final_response" not in turn or not isinstance(turn["final_response"], str):
            missing.append("final_response")
        if missing:
            incomplete.append(f"{label} ({', '.join(missing)})")

    if incomplete:
        details = "; ".join(incomplete)
        raise LauncherError(
            "Recording is missing required conversation content: "
            f"{details}. Export was aborted to avoid creating an incomplete dataset. "
            "If this recording came from Codex, update paper-task and repair the recording "
            "from its Codex session log or record the affected turns again."
        )


def export_dataset(
    workspace_value: str,
    output_value: str | None = None,
    *,
    include_paper_source: bool = True,
    progress: Callable[[str], None] | None = None,
) -> Path:
    workspace = Path(workspace_value).expanduser().resolve()
    if not (workspace / ".git").is_dir():
        raise LauncherError(f"Task workspace is not a Git repository: {workspace}")
    recording_dir = workspace / ".recording"
    manifest = _load_json(recording_dir / "manifest.json")
    output = _validate_output(workspace, output_value)
    turns = _load_jsonl(recording_dir / "transcript.jsonl")
    _validate_required_turn_content(turns)

    baseline = manifest.get("baseline_snapshot")
    if not isinstance(baseline, dict) or not isinstance(baseline.get("commit"), str):
        raise LauncherError("Recording manifest has no valid baseline snapshot")

    normalized_turns: list[dict] = []
    seen_indexes: set[int] = set()
    for position, turn in enumerate(turns, 1):
        snapshot = turn.get("snapshot")
        index = turn.get("turn_index")
        if not isinstance(index, int) or index < 1 or index in seen_indexes:
            raise LauncherError(f"Turn {position} has an invalid or duplicate turn_index")
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("commit"), str):
            raise LauncherError(f"Turn {index} has no valid snapshot commit")
        seen_indexes.add(index)
        record = dict(turn)
        record["code_path"] = f"versions/turn-{index:04d}"
        normalized_turns.append(record)
    normalized_turns.sort(key=lambda item: item["turn_index"])
    expected_indexes = list(range(1, len(normalized_turns) + 1))
    actual_indexes = [turn["turn_index"] for turn in normalized_turns]
    if actual_indexes != expected_indexes:
        raise LauncherError("Recorded turn indexes are not contiguous from 1")
    manifest_turn_count = manifest.get("turn_count")
    if isinstance(manifest_turn_count, int) and manifest_turn_count != len(normalized_turns):
        raise LauncherError(
            "Recording is inconsistent: manifest turn_count does not match transcript.jsonl"
        )

    staging_parent = recording_dir if output == (workspace / "out_data") else output.parent
    staging = Path(tempfile.mkdtemp(prefix=".paper-task-export-", dir=staging_parent))
    try:
        if progress:
            progress("正在校验并导出基线代码…")
        _export_commit(workspace, baseline["commit"], staging / "versions" / "baseline")
        for offset, turn in enumerate(normalized_turns, 1):
            if progress:
                progress(f"正在导出代码版本 {offset}/{len(normalized_turns)}…")
            _export_commit(
                workspace,
                turn["snapshot"]["commit"],
                staging / turn["code_path"],
            )

        paper_exported = False
        paper_source = workspace / "paper-source"
        if include_paper_source and paper_source.is_dir():
            if progress:
                progress("正在复制论文源码…")
            shutil.copytree(paper_source, staging / "paper-source", symlinks=True)
            paper_exported = True

        initial_prompt = recording_dir / "initial-prompt.md"
        if initial_prompt.is_file():
            shutil.copy2(initial_prompt, staging / "initial-prompt.md")
        atomic_json(staging / "recording-manifest.json", _export_manifest(manifest))
        _write_jsonl(staging / "turns.jsonl", normalized_turns)
        dataset = {
            "schema": EXPORT_SCHEMA,
            "exported_at": utc_now(),
            "recording_id": manifest.get("recording_id"),
            "session_id": manifest.get("session_id"),
            "backend": manifest.get("backend", "codex"),
            "recording_state": manifest.get("state"),
            "turn_count": len(normalized_turns),
            "paper": manifest.get("paper"),
            "prompt_template_version": manifest.get("prompt_template_version"),
            "generated_model": manifest.get("generated_model"),
            "modification_model": manifest.get("modification_model"),
            "baseline": {
                "code_path": "versions/baseline",
                "snapshot": baseline,
            },
            "paths": {
                "baseline_code": "versions/baseline",
                "turns": "turns.jsonl",
                "paper_source": "paper-source" if paper_exported else None,
                "initial_prompt": "initial-prompt.md" if initial_prompt.is_file() else None,
                "recording_manifest": "recording-manifest.json",
            },
        }
        atomic_json(staging / "dataset.json", dataset)

        if output.exists():
            output.rmdir()
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if progress:
        progress(f"数据集导出完成：{output}")
    return output
