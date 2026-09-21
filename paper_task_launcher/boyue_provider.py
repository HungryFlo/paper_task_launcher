from __future__ import annotations

import os
import re
import tempfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

from .errors import LauncherError
from .util import utc_now


DEFAULT_BOYUE_URL = "http://35.220.164.252:3888"
DEFAULT_TOKEN_FILE = Path(__file__).resolve().parent.parent / "token_pool" / ".env.token"
PROXY_ENV_KEYS = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
}


@dataclass(frozen=True)
class TokenCandidate:
    label: str
    value: str
    order: int


def load_tokens(path: Path) -> list[TokenCandidate]:
    if not path.is_file():
        raise LauncherError(f"token file not found: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LauncherError(f"Unable to read token file: {path}: {exc}") from exc

    candidates: list[TokenCandidate] = []
    labels: set[str] = set()
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise LauncherError(
                f"Invalid token file at line {line_number}: expected NAME=value"
            )
        label, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not label:
            raise LauncherError(f"Invalid token file at line {line_number}: empty label")
        if label in labels:
            raise LauncherError(
                f"Invalid token file at line {line_number}: duplicate label {label}"
            )
        if not value:
            raise LauncherError(
                f"Invalid token file at line {line_number}: empty token for {label}"
            )
        labels.add(label)
        candidates.append(TokenCandidate(label=label, value=value, order=len(candidates)))
    if not candidates:
        raise LauncherError(f"Token file contains no candidates: {path}")
    return candidates


def normalize_config(
    *, backend: str, model: str, token_file: str, base_url: str
) -> dict:
    if not model.strip():
        raise LauncherError("--model must not be empty")
    normalized_base_url = normalize_base_url(base_url)
    api_base_url = (
        codex_api_base_url(normalized_base_url)
        if backend in {"codex", "kimi"}
        else normalized_base_url
    )
    path = Path(token_file).expanduser().resolve()
    load_tokens(path)
    return {
        "mode": "boyue_token_file",
        "backend": backend,
        "model": model,
        "base_url": normalized_base_url,
        "api_base_url": api_base_url,
        "token_file": str(path),
    }


def direct_connection_environment(
    source: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment that cannot route Boyue traffic through a proxy."""
    env = (source if source is not None else os.environ).copy()
    for key in PROXY_ENV_KEYS:
        env.pop(key, None)
    # Codex talks to a task-owned loopback relay. Keep this explicit for any
    # HTTP stack that independently consults the no-proxy variables.
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    return env


def normalize_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized:
        raise LauncherError("--boyue-url must not be empty")
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LauncherError("--boyue-url must be an absolute http:// or https:// URL")
    if parsed.query or parsed.fragment:
        raise LauncherError("--boyue-url must not contain a query or fragment")
    return normalized


def codex_api_base_url(base_url: str) -> str:
    normalized = normalize_base_url(base_url)
    if normalized.lower().endswith("/v1"):
        return normalized
    return normalized + "/v1"


def validate_stored_config(value: object, *, backend: str | None = None) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("mode") != "boyue_token_file":
        raise LauncherError("Recording manifest has an invalid Boyue provider configuration")
    required = ("backend", "model", "base_url", "token_file")
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in required):
        raise LauncherError("Recording manifest has an incomplete Boyue provider configuration")
    if value["backend"] not in {"codex", "claude", "kimi"}:
        raise LauncherError("Recording manifest has an unsupported Boyue backend")
    if backend is not None and value["backend"] != backend:
        raise LauncherError("Recording Boyue provider does not match the recorded backend")
    value["base_url"] = normalize_base_url(value["base_url"])
    expected_api_url = (
        codex_api_base_url(value["base_url"])
        if value["backend"] in {"codex", "kimi"}
        else value["base_url"]
    )
    api_base_url = value.get("api_base_url")
    if api_base_url is None:
        value["api_base_url"] = expected_api_url
    elif not isinstance(api_base_url, str) or not api_base_url.strip():
        raise LauncherError("Recording manifest has an invalid Boyue API base URL")
    else:
        value["api_base_url"] = normalize_base_url(api_base_url)
    load_tokens(Path(value["token_file"]))
    return value


def _safe_label(label: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "?", label)


Probe = Callable[
    [TokenCandidate, dict, str, float, Event],
    tuple[TokenCandidate, bool, str, float],
]


def select_token(
    config: dict,
    executable: str,
    probe: Probe,
    *,
    provider_name: str,
    timeout: float = 90.0,
    workers: int = 0,
    progress: Callable[[str], None] | None = None,
) -> TokenCandidate:
    candidates = load_tokens(Path(str(config["token_file"])))
    worker_count = max(1, min(workers or len(candidates), len(candidates)))
    if progress:
        progress(
            f"正在测试 {len(candidates)} 个 {provider_name} token"
            f"（并发={worker_count}，token 不会显示）…"
        )
    stop_event = Event()
    selected: TokenCandidate | None = None
    pool = ThreadPoolExecutor(max_workers=worker_count)
    futures = []
    try:
        futures = [
            pool.submit(probe, candidate, config, executable, timeout, stop_event)
            for candidate in candidates
        ]
        for future in as_completed(futures):
            candidate, success, detail, duration = future.result()
            if progress:
                state = "PASS" if success else "FAIL"
                progress(
                    f"token {_safe_label(candidate.label)}: {state} "
                    f"({detail}, {duration:.2f}s)"
                )
            if success:
                selected = candidate
                stop_event.set()
                break
    finally:
        stop_event.set()
        for future in futures:
            future.cancel()
        # Running probes cooperatively terminate their child process when the
        # event is set, so waiting here normally takes only one polling tick.
        pool.shutdown(wait=True, cancel_futures=True)
    if selected is None:
        raise LauncherError(f"No usable token was found for {provider_name}")
    if progress:
        progress(f"已选择 token 标签：{_safe_label(selected.label)}（token 不会显示）")
    return selected


def record_token_selection(config: dict, selected: TokenCandidate) -> None:
    config["last_selected_token_label"] = selected.label
    config["last_token_tested_at"] = utc_now()


def write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
