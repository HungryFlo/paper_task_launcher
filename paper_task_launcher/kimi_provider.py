from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path
from threading import Event
from typing import Callable

from .boyue_provider import (
    TokenCandidate,
    codex_api_base_url,
    direct_connection_environment,
    record_token_selection,
    select_token as select_boyue_token,
    write_private_text,
)


TOKEN_ENV_KEY = "PAPER_TASK_BOYUE_TOKEN"
PROVIDER_ID = "paper-task"
MAX_CONTEXT_SIZE = 1_048_576


def model_alias(config: dict) -> str:
    return f"{PROVIDER_ID}/{config['model']}"


def render_config(config: dict) -> str:
    alias = model_alias(config)
    base_url = str(
        config.get("api_base_url") or codex_api_base_url(str(config["base_url"]))
    )
    text = (
        f"default_model = {json.dumps(alias, ensure_ascii=False)}\n"
        "telemetry = false\n\n"
        f"[providers.{PROVIDER_ID}]\n"
        'type = "kimi"\n'
        f"base_url = {json.dumps(base_url, ensure_ascii=False)}\n"
        f"api_key_env = {json.dumps(TOKEN_ENV_KEY)}\n\n"
        f"[models.{json.dumps(alias, ensure_ascii=False)}]\n"
        f"provider = {json.dumps(PROVIDER_ID)}\n"
        f"model = {json.dumps(str(config['model']), ensure_ascii=False)}\n"
        f"max_context_size = {MAX_CONTEXT_SIZE}\n"
        'capabilities = ["thinking", "tool_use"]\n'
        f"display_name = {json.dumps(str(config['model']), ensure_ascii=False)}\n"
    )
    # Validate locally without starting Kimi or making a network request.
    tomllib.loads(text)
    return text


def write_config(config_dir: Path, config: dict) -> Path:
    path = config_dir / "config.toml"
    write_private_text(path, render_config(config))
    return path


def session_environment(config: dict, token: str, config_dir: Path) -> dict[str, str]:
    env = direct_connection_environment()
    for key in list(env):
        if key.startswith("KIMI_MODEL_") or key in {
            "KIMI_API_KEY",
            "KIMI_BASE_URL",
            "KIMI_CODE_HOME",
            TOKEN_ENV_KEY,
        }:
            env.pop(key, None)
    env["KIMI_CODE_HOME"] = str(config_dir.resolve())
    env[TOKEN_ENV_KEY] = token
    env["NO_COLOR"] = "1"
    # File watching is unnecessary for an isolated task and can exhaust the
    # macOS file descriptor limit in large workspaces.
    env["KIMI_CODE_WATCH"] = "0"
    return env


def _response_is_nonempty(value: str) -> bool:
    return bool(re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value).strip())


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()


def _probe_one(
    candidate: TokenCandidate,
    config: dict,
    kimi_bin: str,
    timeout: float,
    stop_event: Event,
) -> tuple[TokenCandidate, bool, str, float]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="paper-task-kimi-probe-") as temporary:
        root = Path(temporary)
        config_dir = root / "kimi-home"
        work_dir = root / "workspace"
        config_dir.mkdir(mode=0o700)
        work_dir.mkdir(mode=0o700)
        write_config(config_dir, config)
        env = session_environment(config, candidate.value, config_dir)
        command = [
            kimi_bin,
            "--prompt",
            "Reply with exactly OK and nothing else.",
            "--model",
            model_alias(config),
            "--output-format",
            "text",
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
                    _stop_process(process)
                    return (
                        candidate,
                        False,
                        "cancelled after another token passed",
                        time.monotonic() - started,
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _stop_process(process)
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
            if process.returncode == 0 and _response_is_nonempty(stdout):
                success, detail = True, "success"
            elif process.returncode != 0:
                success, detail = False, f"Kimi Code exit={process.returncode}"
            else:
                success, detail = False, "empty probe response"
        except FileNotFoundError:
            success, detail = False, "Kimi Code executable not found"
        except OSError as exc:
            success, detail = False, f"unable to start Kimi Code ({type(exc).__name__})"
        finally:
            if process is not None:
                _stop_process(process)
    return candidate, success, detail, time.monotonic() - started


def select_token(
    config: dict,
    kimi_bin: str,
    *,
    timeout: float = 90.0,
    workers: int = 0,
    progress: Callable[[str], None] | None = None,
) -> TokenCandidate:
    return select_boyue_token(
        config,
        kimi_bin,
        _probe_one,
        provider_name="Kimi Code",
        timeout=timeout,
        workers=workers,
        progress=progress,
    )


__all__ = [
    "MAX_CONTEXT_SIZE",
    "PROVIDER_ID",
    "TOKEN_ENV_KEY",
    "model_alias",
    "record_token_selection",
    "render_config",
    "select_token",
    "session_environment",
    "write_config",
]
