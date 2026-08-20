from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import LauncherError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def directory_digest(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
        count += 1
    return digest.hexdigest(), count


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def run_checked(
    args: Iterable[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> str:
    try:
        result = subprocess.run(
            list(args),
            cwd=cwd,
            env=env,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise LauncherError(f"Command not found: {exc.filename}") from exc
    if result.returncode != 0:
        command = " ".join(args)
        detail = result.stderr.strip() or result.stdout.strip()
        raise LauncherError(f"Command failed ({command}): {detail}")
    return result.stdout.strip()

