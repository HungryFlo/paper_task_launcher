from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Event
from typing import Callable

from .boyue_provider import (
    TokenCandidate,
    direct_connection_environment,
    load_tokens,
    normalize_base_url,
    record_token_selection,
    select_token as select_boyue_token,
    write_private_text,
)
from .errors import LauncherError


SUPPORTED_PROFILES = {"claude", "kimi"}
MANAGED_ENV_KEYS = {
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "API_TIMEOUT_MS",
    "CLAUDE_CONFIG_DIR",
}


def managed_config(
    *,
    backend: str,
    model: str | None,
    token_file: str | None,
    base_url: str | None,
) -> dict | None:
    """Build the legacy managed-Claude configuration."""
    values = {
        "--claude-model": model,
        "--token-file": token_file,
        "--boyue-url": base_url,
    }
    supplied = [name for name, value in values.items() if value is not None]
    if not supplied:
        return None
    if backend != "claude":
        raise LauncherError("Claude provider options require --backend claude")
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise LauncherError(
            "Managed Claude authentication requires all of "
            "--claude-model, --token-file, and --boyue-url; "
            f"missing: {', '.join(missing)}"
        )

    normalized_model = str(model).strip()
    if not normalized_model:
        raise LauncherError("--claude-model must not be empty")
    normalized_base_url = normalize_base_url(str(base_url))
    path = Path(str(token_file)).expanduser().resolve()
    load_tokens(path)
    return {
        "mode": "token_file",
        "profile": "kimi" if normalized_model.lower().startswith("kimi") else "claude",
        "model": normalized_model,
        "base_url": normalized_base_url,
        "token_file": str(path),
    }


def boyue_claude_config(config: dict) -> dict:
    result = dict(config)
    result["profile"] = (
        "kimi" if str(result["model"]).lower().startswith("kimi") else "claude"
    )
    return result


def validate_stored_config(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("mode") != "token_file":
        raise LauncherError("Recording manifest has an invalid Claude provider configuration")
    required = ("profile", "model", "base_url", "token_file")
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in required):
        raise LauncherError("Recording manifest has an incomplete Claude provider configuration")
    if value["profile"] not in SUPPORTED_PROFILES:
        raise LauncherError("Recording manifest has an unsupported Claude provider profile")
    load_tokens(Path(value["token_file"]))
    return value


def profile_environment(config: dict, token: str) -> dict[str, str]:
    result = {
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_BASE_URL": str(config["base_url"]),
    }
    if config.get("profile") == "kimi":
        model = str(config["model"])
        result.update(
            {
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "API_TIMEOUT_MS": "3000000",
            }
        )
    return result


def _clean_environment() -> dict[str, str]:
    env = direct_connection_environment()
    for key in MANAGED_ENV_KEYS:
        env.pop(key, None)
    return env


def session_environment(config: dict, token: str, config_dir: Path) -> dict[str, str]:
    env = _clean_environment()
    env.update(profile_environment(config, token))
    env["CLAUDE_CONFIG_DIR"] = str(config_dir.resolve())
    return env


def _response_is_ok(stdout: str) -> bool:
    text = stdout.strip()
    if not text:
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text.upper() == "OK"
    return isinstance(payload, dict) and isinstance(payload.get("result"), str) and (
        payload["result"].strip().upper() == "OK"
    )


def _probe_one(
    candidate: TokenCandidate,
    config: dict,
    claude_bin: str,
    timeout: float,
    stop_event: Event,
) -> tuple[TokenCandidate, bool, str, float]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="paper-task-claude-probe-") as temporary:
        root = Path(temporary)
        config_dir = root / "config"
        work_dir = root / "workspace"
        config_dir.mkdir(mode=0o700)
        work_dir.mkdir(mode=0o700)
        env = session_environment(config, candidate.value, config_dir)
        command = [
            claude_bin,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
            "--model",
            str(config["model"]),
            "Reply with exactly OK and nothing else.",
        ]
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=work_dir,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = started + timeout
            stdout = ""
            while True:
                if stop_event.is_set():
                    process.terminate()
                    try:
                        process.communicate(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    return (
                        candidate,
                        False,
                        "cancelled after another token passed",
                        time.monotonic() - started,
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.terminate()
                    try:
                        process.communicate(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    return (
                        candidate,
                        False,
                        f"timeout after {timeout:g}s",
                        time.monotonic() - started,
                    )
                try:
                    stdout, _stderr = process.communicate(
                        timeout=min(0.1, remaining)
                    )
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode == 0 and _response_is_ok(stdout):
                success, detail = True, "success"
            elif process.returncode != 0:
                success, detail = False, f"Claude Code exit={process.returncode}"
            else:
                success, detail = False, "unexpected probe response"
        except FileNotFoundError:
            success, detail = False, "Claude Code executable not found"
        except OSError as exc:
            success, detail = False, f"unable to start Claude Code ({type(exc).__name__})"
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
    return candidate, success, detail, time.monotonic() - started


def select_token(
    config: dict,
    claude_bin: str,
    *,
    timeout: float = 90.0,
    workers: int = 0,
    progress: Callable[[str], None] | None = None,
) -> TokenCandidate:
    return select_boyue_token(
        config,
        claude_bin,
        _probe_one,
        provider_name="Claude",
        timeout=timeout,
        workers=workers,
        progress=progress,
    )


def write_private_json(path: Path, value: dict) -> None:
    write_private_text(
        path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


__all__ = [
    "TokenCandidate",
    "boyue_claude_config",
    "load_tokens",
    "managed_config",
    "profile_environment",
    "record_token_selection",
    "select_token",
    "session_environment",
    "validate_stored_config",
    "write_private_json",
]
