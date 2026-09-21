from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .git_history import SnapshotStore
from .util import append_jsonl, atomic_json, utc_now


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(part for part in parts if part)


def _time_text(value: object) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()


class KimiEventParser:
    def __init__(self, *, session_id: str, on_turn: Callable[[dict], None]):
        self.session_id = session_id
        self.on_turn = on_turn
        self.pending_prompts: list[tuple[str, str | None]] = []
        self.text_by_step: dict[tuple[str, int], list[str]] = {}
        self.final_steps: dict[str, set[int]] = {}

    def feed(self, record: dict) -> None:
        record_type = record.get("type")
        if record_type == "turn.prompt" and record.get("agentId") == "main":
            self.pending_prompts.append(
                (_content_text(record.get("input")), _time_text(record.get("time")))
            )
            return
        if record_type == "context.append_loop_event":
            event = record.get("event")
            if not isinstance(event, dict):
                return
            turn_id = str(event.get("turnId", ""))
            step = event.get("step")
            if not turn_id or not isinstance(step, int):
                return
            if event.get("type") == "content.part":
                part = event.get("part")
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    self.text_by_step.setdefault((turn_id, step), []).append(part["text"])
            elif event.get("type") == "step.end" and event.get("finishReason") == "end_turn":
                self.final_steps.setdefault(turn_id, set()).add(step)
            return
        if record_type != "turn.ended" or record.get("agentId") != "main":
            return
        raw_turn_id = str(record.get("turnId", ""))
        if not raw_turn_id:
            return
        if self.pending_prompts:
            user_input, started_at = self.pending_prompts.pop(0)
        else:
            user_input, started_at = "", None
        final_candidates = self.final_steps.pop(raw_turn_id, set())
        if final_candidates:
            final_step = max(final_candidates)
        else:
            steps = [step for turn, step in self.text_by_step if turn == raw_turn_id]
            final_step = max(steps) if steps else -1
        final_response = "\n".join(
            self.text_by_step.pop((raw_turn_id, final_step), [])
        ).strip()
        for key in [key for key in self.text_by_step if key[0] == raw_turn_id]:
            self.text_by_step.pop(key, None)
        self.on_turn(
            {
                "turn_id": f"{self.session_id}:{raw_turn_id}",
                "user_input": user_input,
                "final_response": final_response,
                "started_at": started_at,
                "completed_at": _time_text(record.get("time")),
                "duration_ms": record.get("durationMs"),
                "finish_reason": record.get("reason"),
            }
        )


class KimiSessionRecorder(threading.Thread):
    def __init__(
        self,
        *,
        workspace: Path,
        recording_dir: Path,
        recording_id: str,
        manifest: dict,
        config_dir: Path,
        launch_time: float,
        resume_session_id: str | None = None,
        resume_offset: int = 0,
        poll_interval: float = 0.2,
    ):
        super().__init__(name="paper-task-kimi-recorder", daemon=True)
        self.workspace = workspace.resolve()
        self.recording_dir = recording_dir
        self.recording_id = recording_id
        self.manifest = manifest
        self.config_dir = config_dir
        self.launch_time = launch_time
        self.resume_session_id = resume_session_id
        self.resume_offset = max(0, resume_offset)
        self.poll_interval = poll_interval
        self.stop_requested = threading.Event()
        self.log_path: Path | None = None
        self.session_id: str | None = None
        self.offset = 0
        self.partial = ""
        self.error: Exception | None = None
        self.recorded_turn_ids: set[str] = set()
        transcript_path = recording_dir / "transcript.jsonl"
        if transcript_path.is_file():
            for line in transcript_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                turn_id = value.get("turn_id")
                if isinstance(turn_id, str):
                    self.recorded_turn_ids.add(turn_id)
        self.snapshots = SnapshotStore(
            workspace,
            recording_id,
            snapshot_policy=str(manifest.get("snapshot_policy", "git")),
        )
        count = manifest.get("turn_count", 0)
        self.snapshots.count = count if isinstance(count, int) else 0
        previous = manifest.get("latest_snapshot") or manifest.get("baseline_snapshot")
        if isinstance(previous, dict) and isinstance(previous.get("commit"), str):
            self.snapshots.parent = previous["commit"]
        self.parser: KimiEventParser | None = None

    def request_stop(self) -> None:
        self.stop_requested.set()

    def _candidate(self, state_path: Path) -> tuple[Path, str] | None:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            session_id = state.get("id")
            cwd = state.get("cwd")
            log_path = state_path.parent / "agents" / "main" / "wire.jsonl"
            if not isinstance(session_id, str) or not isinstance(cwd, str):
                return None
            if Path(cwd).resolve() != self.workspace or not log_path.is_file():
                return None
            if self.resume_session_id:
                if session_id != self.resume_session_id:
                    return None
            elif state_path.stat().st_mtime < self.launch_time - 1:
                return None
            return log_path, session_id
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _discover(self) -> tuple[Path, str] | None:
        root = self.config_dir / "sessions"
        if not root.is_dir():
            return None
        matches = [
            match
            for path in root.glob("*/session_*/state.json")
            if (match := self._candidate(path)) is not None
        ]
        if not matches:
            return None
        matches.sort(key=lambda item: item[0].stat().st_mtime)
        return matches[-1]

    def _on_turn(self, turn: dict) -> None:
        turn_id = turn.get("turn_id")
        if not isinstance(turn_id, str) or turn_id in self.recorded_turn_ids:
            return
        snapshot = self.snapshots.capture(
            f"turn-{self.snapshots.count + 1:04d}", turn_id=turn_id
        )
        turn["turn_index"] = self.snapshots.count
        model = self.manifest.get("modification_model")
        if isinstance(model, str) and model:
            turn["model"] = model
        turn["snapshot"] = snapshot
        turn["recorded_at"] = utc_now()
        append_jsonl(self.recording_dir / "transcript.jsonl", turn)
        append_jsonl(self.recording_dir / "snapshots.jsonl", snapshot)
        self.recorded_turn_ids.add(turn_id)
        self.manifest["turn_count"] = self.snapshots.count
        self.manifest["latest_snapshot"] = snapshot
        atomic_json(self.recording_dir / "manifest.json", self.manifest)

    def _bind(self, log_path: Path, session_id: str) -> None:
        self.log_path = log_path
        self.session_id = session_id
        self.offset = min(self.resume_offset, log_path.stat().st_size)
        self.parser = KimiEventParser(session_id=session_id, on_turn=self._on_turn)
        self.manifest["session_id"] = session_id
        self.manifest["session_log"] = str(log_path)
        atomic_json(self.recording_dir / "manifest.json", self.manifest)

    def _consume(self) -> None:
        assert self.log_path is not None and self.parser is not None
        with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self.offset)
            chunk = handle.read()
            self.offset = handle.tell()
        if not chunk:
            return
        lines = (self.partial + chunk).splitlines(keepends=True)
        self.partial = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.partial = lines.pop()
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                self.parser.feed(value)
        if not self.partial:
            self.manifest["session_log_offset"] = self.offset
            atomic_json(self.recording_dir / "manifest.json", self.manifest)

    def run(self) -> None:
        try:
            while self.log_path is None and not self.stop_requested.is_set():
                discovered = self._discover()
                if discovered is not None:
                    self._bind(*discovered)
                    break
                time.sleep(self.poll_interval)
            while self.log_path is not None and not self.stop_requested.is_set():
                self._consume()
                time.sleep(self.poll_interval)
            if self.log_path is None:
                discovered = self._discover()
                if discovered is not None:
                    self._bind(*discovered)
            if self.log_path is not None:
                self._consume()
        except Exception as exc:
            self.error = exc


__all__ = ["KimiEventParser", "KimiSessionRecorder"]
