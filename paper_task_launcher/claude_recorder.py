from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import LauncherError
from .git_history import SnapshotStore
from .util import append_jsonl, atomic_json, utc_now


def _load_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LauncherError(f"Claude recorder file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LauncherError(f"Unable to read Claude recorder file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LauncherError(f"Claude recorder file must contain a JSON object: {path}")
    return value


def _state(recording_dir: Path) -> dict:
    path = recording_dir / "claude-state.json"
    if not path.exists():
        return {"pending_turn": None}
    return _load_object(path)


def _validate_event(workspace: Path, recording_dir: Path, event: dict) -> None:
    if recording_dir.resolve() != (workspace / ".recording").resolve():
        raise LauncherError("Claude recorder directory does not belong to the task workspace")
    if event.get("hook_event_name") == "SessionStart":
        cwd = event.get("cwd")
        if not isinstance(cwd, str) or Path(cwd).resolve() != workspace:
            raise LauncherError("Claude hook session belongs to a different workspace")


def handle_claude_hook(workspace: Path, recording_dir: Path, event: dict) -> None:
    workspace = workspace.resolve()
    recording_dir = recording_dir.resolve()
    _validate_event(workspace, recording_dir, event)
    event_name = event.get("hook_event_name")
    manifest_path = recording_dir / "manifest.json"
    manifest = _load_object(manifest_path)
    session_id = event.get("session_id")
    recorded_session_id = manifest.get("session_id")
    if event_name != "SessionStart" and recorded_session_id and session_id != recorded_session_id:
        raise LauncherError("Claude hook event belongs to a different session")

    if event_name == "SessionStart":
        if recorded_session_id and session_id != recorded_session_id:
            raise LauncherError("Claude resumed a different session than requested")
        manifest["session_id"] = session_id
        manifest["session_log"] = event.get("transcript_path")
        if isinstance(event.get("model"), str):
            manifest["claude_model"] = event["model"]
        atomic_json(manifest_path, manifest)
        return

    if event_name == "UserPromptSubmit":
        prompt = event.get("prompt")
        if not isinstance(prompt, str):
            raise LauncherError("Claude UserPromptSubmit event has no prompt text")
        state = _state(recording_dir)
        state["pending_turn"] = {
            "prompt_id": event.get("prompt_id"),
            "session_id": session_id,
            "user_input": prompt,
            "started_at": utc_now(),
        }
        atomic_json(recording_dir / "claude-state.json", state)
        return

    if event_name != "Stop":
        return

    state = _state(recording_dir)
    pending = state.get("pending_turn")
    if not isinstance(pending, dict):
        raise LauncherError("Claude Stop event has no matching submitted user prompt")
    prompt_id = event.get("prompt_id")
    pending_prompt_id = pending.get("prompt_id")
    if prompt_id and pending_prompt_id and prompt_id != pending_prompt_id:
        raise LauncherError("Claude Stop event prompt_id does not match the pending turn")
    final_response = event.get("last_assistant_message")
    if not isinstance(final_response, str):
        raise LauncherError("Claude Stop event has no final assistant message")

    count = manifest.get("turn_count", 0)
    if not isinstance(count, int) or count < 0:
        raise LauncherError("Recording manifest has an invalid turn count")
    snapshots = SnapshotStore(
        workspace,
        str(manifest.get("recording_id")),
        snapshot_policy=str(manifest.get("snapshot_policy", "git")),
    )
    snapshots.count = count
    previous = manifest.get("latest_snapshot") or manifest.get("baseline_snapshot")
    if not isinstance(previous, dict) or not isinstance(previous.get("commit"), str):
        raise LauncherError("Recording manifest has no valid previous snapshot")
    snapshots.parent = previous["commit"]
    turn_id = str(prompt_id or f"claude-turn-{count + 1:04d}")
    snapshot = snapshots.capture(f"turn-{count + 1:04d}", turn_id=turn_id)
    completed_at = utc_now()
    turn = {
        "turn_id": turn_id,
        "turn_index": snapshots.count,
        "user_input": pending.get("user_input", ""),
        "final_response": final_response,
        "started_at": pending.get("started_at"),
        "completed_at": completed_at,
        "duration_ms": None,
        "snapshot": snapshot,
        "recorded_at": completed_at,
    }
    model = manifest.get("modification_model")
    if isinstance(model, str) and model:
        turn["model"] = model
    append_jsonl(recording_dir / "transcript.jsonl", turn)
    append_jsonl(recording_dir / "snapshots.jsonl", snapshot)
    manifest["session_id"] = session_id or manifest.get("session_id")
    manifest["turn_count"] = snapshots.count
    manifest["latest_snapshot"] = snapshot
    atomic_json(manifest_path, manifest)
    state["pending_turn"] = None
    atomic_json(recording_dir / "claude-state.json", state)


def build_claude_settings(workspace: Path, recording_dir: Path) -> dict:
    package_root = str(Path(__file__).resolve().parent.parent)
    bootstrap = (
        "import runpy,sys;"
        "sys.path.insert(0,sys.argv.pop(1));"
        "sys.argv[0]='paper-task-claude-recorder';"
        "runpy.run_module('paper_task_launcher.claude_recorder',run_name='__main__')"
    )
    args = [
        "-c",
        bootstrap,
        package_root,
        "--workspace",
        str(workspace.resolve()),
        "--recording-dir",
        str(recording_dir.resolve()),
    ]
    handler = {
        "type": "command",
        "command": sys.executable,
        "args": args,
        "timeout": 30,
    }
    return {
        "hooks": {
            event: [{"hooks": [handler]}]
            for event in ("SessionStart", "UserPromptSubmit", "Stop")
        }
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paper-task-claude-recorder")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--recording-dir", required=True)
    args = parser.parse_args(argv)
    recording_dir = Path(args.recording_dir)
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise LauncherError("Claude hook input must be a JSON object")
        handle_claude_hook(Path(args.workspace), recording_dir, event)
    except Exception as exc:
        # A recorder failure must never block or alter Claude Code's response.
        try:
            append_jsonl(
                recording_dir / "hook-errors.jsonl",
                {"recorded_at": utc_now(), "error": str(exc)},
            )
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
