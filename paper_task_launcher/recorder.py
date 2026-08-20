from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

from .git_history import SnapshotStore
from .util import append_jsonl, atomic_json, utc_now


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "\n".join(part for part in parts if part)


class SessionEventParser:
    def __init__(
        self,
        *,
        on_session: Callable[[dict], None],
        on_turn: Callable[[dict], None],
    ):
        self.on_session = on_session
        self.on_turn = on_turn
        self.pending_users: dict[str, str] = {}
        self.current_turn: str | None = None
        self.fallback_user: str | None = None

    def feed(self, record: dict) -> None:
        record_type = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        if record_type == "session_meta":
            self.on_session(payload)
            return
        if record_type != "event_msg":
            return
        event_type = payload.get("type")
        if event_type == "task_started":
            self.current_turn = payload.get("turn_id")
            return
        if event_type == "item_completed":
            item = payload.get("item")
            if not isinstance(item, dict) or item.get("type") != "UserMessage":
                return
            text = content_text(item.get("content"))
            turn_id = payload.get("turn_id") or self.current_turn
            if turn_id:
                self.pending_users[str(turn_id)] = text
            else:
                self.fallback_user = text
            return
        if event_type != "task_complete":
            return
        turn_id = str(payload.get("turn_id") or self.current_turn or "")
        user_input = self.pending_users.pop(turn_id, self.fallback_user or "")
        self.fallback_user = None
        final_response = payload.get("last_agent_message")
        if not isinstance(final_response, str):
            final_response = ""
        self.on_turn(
            {
                "turn_id": turn_id,
                "user_input": user_input,
                "final_response": final_response,
                "started_at": payload.get("started_at"),
                "completed_at": payload.get("completed_at") or record.get("timestamp"),
                "duration_ms": payload.get("duration_ms"),
            }
        )


class SessionRecorder(threading.Thread):
    def __init__(
        self,
        *,
        workspace: Path,
        recording_dir: Path,
        recording_id: str,
        manifest: dict,
        launch_time: float,
        existing_logs: set[Path],
        poll_interval: float = 0.2,
    ):
        super().__init__(name="paper-task-session-recorder", daemon=True)
        self.workspace = workspace.resolve()
        self.recording_dir = recording_dir
        self.recording_id = recording_id
        self.manifest = manifest
        self.launch_time = launch_time
        self.existing_logs = existing_logs
        self.poll_interval = poll_interval
        self.stop_requested = threading.Event()
        self.log_path: Path | None = None
        self.offset = 0
        self.partial = ""
        self.error: Exception | None = None
        self.snapshots = SnapshotStore(workspace, recording_id)
        baseline = manifest.get("baseline_snapshot")
        if isinstance(baseline, dict) and isinstance(baseline.get("commit"), str):
            self.snapshots.parent = baseline["commit"]
        self.parser = SessionEventParser(
            on_session=self._on_session,
            on_turn=self._on_turn,
        )

    @staticmethod
    def sessions_root() -> Path:
        configured = os.environ.get("CODEX_HOME")
        root = Path(configured).expanduser() if configured else Path.home() / ".codex"
        return root / "sessions"

    @classmethod
    def current_logs(cls) -> set[Path]:
        root = cls.sessions_root()
        return set(root.rglob("*.jsonl")) if root.exists() else set()

    def request_stop(self) -> None:
        self.stop_requested.set()

    def _candidate_matches(self, path: Path) -> bool:
        try:
            if path in self.existing_logs or path.stat().st_mtime < self.launch_time - 1:
                return False
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                first = handle.readline()
            record = json.loads(first)
            payload = record.get("payload", {})
            cwd = payload.get("cwd")
            return record.get("type") == "session_meta" and cwd and Path(cwd).resolve() == self.workspace
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    def _discover(self) -> Path | None:
        root = self.sessions_root()
        if not root.exists():
            return None
        candidates = [path for path in root.rglob("*.jsonl") if self._candidate_matches(path)]
        if not candidates:
            return None
        candidates.sort(key=lambda path: path.stat().st_mtime)
        return candidates[0]

    def _on_session(self, payload: dict) -> None:
        self.manifest["session_id"] = payload.get("session_id") or payload.get("id")
        self.manifest["codex_cli_version"] = payload.get("cli_version")
        self.manifest["session_log"] = str(self.log_path) if self.log_path else None
        atomic_json(self.recording_dir / "manifest.json", self.manifest)

    def _on_turn(self, turn: dict) -> None:
        snapshot = self.snapshots.capture(
            f"turn-{self.snapshots.count + 1:04d}", turn_id=turn.get("turn_id")
        )
        turn["turn_index"] = self.snapshots.count
        turn["snapshot"] = snapshot
        turn["recorded_at"] = utc_now()
        append_jsonl(self.recording_dir / "transcript.jsonl", turn)
        append_jsonl(self.recording_dir / "snapshots.jsonl", snapshot)
        self.manifest["turn_count"] = self.snapshots.count
        self.manifest["latest_snapshot"] = snapshot
        atomic_json(self.recording_dir / "manifest.json", self.manifest)

    def _consume(self) -> None:
        assert self.log_path is not None
        with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self.offset)
            chunk = handle.read()
            self.offset = handle.tell()
        if not chunk:
            return
        data = self.partial + chunk
        lines = data.splitlines(keepends=True)
        self.partial = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.partial = lines.pop()
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.parser.feed(record)

    def run(self) -> None:
        try:
            while self.log_path is None and not self.stop_requested.is_set():
                self.log_path = self._discover()
                if self.log_path is None:
                    time.sleep(self.poll_interval)
            while self.log_path is not None and not self.stop_requested.is_set():
                self._consume()
                time.sleep(self.poll_interval)
            if self.log_path is None:
                # A very short-lived Codex invocation can exit before the discovery poll.
                self.log_path = self._discover()
            if self.log_path is not None:
                self._consume()
        except Exception as exc:
            self.error = exc
