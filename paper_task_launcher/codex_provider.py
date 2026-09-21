from __future__ import annotations

import json
import http.server
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
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
from .errors import LauncherError


TOKEN_ENV_KEY = "PAPER_TASK_BOYUE_TOKEN"
_HOP_BY_HOP_HEADERS = {
    "connection",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class ForwardingRelay:
    def __init__(
        self,
        base_url: str,
        server: http.server.ThreadingHTTPServer,
        thread: threading.Thread,
    ) -> None:
        self.base_url = base_url
        self._server = server
        self._thread = thread
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def start_forwarding_relay(upstream_base_url: str) -> ForwardingRelay:
    parsed = urllib.parse.urlsplit(upstream_base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LauncherError(f"Unsupported Codex API base URL: {upstream_base_url}")
    if parsed.query or parsed.fragment:
        raise LauncherError("Codex API base URL cannot contain a query or fragment")
    upstream_origin = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "", "", "")
    )
    base_path = parsed.path.rstrip("/")

    class RelayHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _send_relay_error(self, exc: BaseException) -> None:
            payload = json.dumps(
                {
                    "error": {
                        "type": "local_relay_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True

        def _forward(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else None
            except (OSError, ValueError) as exc:
                self._send_relay_error(exc)
                return
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in _HOP_BY_HOP_HEADERS
                and name.lower() != "content-length"
            }
            request = urllib.request.Request(
                upstream_origin + self.path,
                data=body,
                headers=headers,
                method=self.command,
            )
            try:
                # The user's interactive shell may be configured with a proxy,
                # but Boyue is only reachable directly. An empty ProxyHandler
                # prevents urllib from consulting HTTP(S)_PROXY here.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                response = opener.open(request, timeout=600)
            except urllib.error.HTTPError as exc:
                response = exc
            except Exception as exc:
                self._send_relay_error(exc)
                return
            try:
                self.send_response(getattr(response, "status", response.code))
                for name, value in response.headers.items():
                    if name.lower() not in _HOP_BY_HOP_HEADERS:
                        self.send_header(name, value)
                self.send_header("Connection", "close")
                self.end_headers()
                read_chunk = getattr(response, "read1", response.read)
                while chunk := read_chunk(64 * 1024):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                response.close()
                self.close_connection = True

        def do_GET(self) -> None:  # noqa: N802
            self._forward()

        def do_POST(self) -> None:  # noqa: N802
            self._forward()

    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
    except OSError as exc:
        raise LauncherError(f"Unable to start local Codex relay: {exc}") from exc
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever,
        name="paper-task-codex-relay",
        daemon=True,
    )
    thread.start()
    local_base_url = f"http://127.0.0.1:{server.server_port}{base_path}"
    return ForwardingRelay(local_base_url, server, thread)


@contextmanager
def forwarding_relay(upstream_base_url: str):
    relay = start_forwarding_relay(upstream_base_url)
    try:
        yield relay
    finally:
        relay.close()


def relay_config_override(base_url: str) -> str:
    return f"model_providers.boyue.base_url={json.dumps(base_url)}"


def render_config(config: dict, workspace: Path | None = None) -> str:
    model = json.dumps(str(config["model"]), ensure_ascii=False)
    effective_base_url = config.get("api_base_url") or codex_api_base_url(
        str(config["base_url"])
    )
    base_url = json.dumps(str(effective_base_url), ensure_ascii=False)
    result = (
        f"model = {model}\n"
        'model_provider = "boyue"\n\n'
        '[model_providers.boyue]\n'
        'name = "Boyue"\n'
        f"base_url = {base_url}\n"
        f'env_key = "{TOKEN_ENV_KEY}"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = false\n'
        'request_max_retries = 2\n'
        'stream_max_retries = 2\n'
        'stream_idle_timeout_ms = 300000\n'
        '\n[shell_environment_policy]\n'
        f'exclude = ["{TOKEN_ENV_KEY}"]\n'
    )
    if workspace is not None:
        project = json.dumps(str(workspace.resolve()), ensure_ascii=False)
        result += f'\n[projects.{project}]\ntrust_level = "trusted"\n'
    return result


def write_config(
    config_dir: Path, config: dict, *, workspace: Path | None = None
) -> Path:
    path = config_dir / "config.toml"
    write_private_text(path, render_config(config, workspace))
    return path


def session_environment(config: dict, token: str, config_dir: Path) -> dict[str, str]:
    env = direct_connection_environment()
    for key in list(env):
        if key.startswith("CODEX_") or key in {"OPENAI_API_KEY", TOKEN_ENV_KEY}:
            env.pop(key, None)
    env["CODEX_HOME"] = str(config_dir.resolve())
    env["CODEX_SQLITE_HOME"] = str(config_dir.resolve())
    env[TOKEN_ENV_KEY] = token
    env["NO_COLOR"] = "1"
    return env


def _probe_one(
    candidate: TokenCandidate,
    config: dict,
    codex_bin: str,
    timeout: float,
    stop_event: Event,
) -> tuple[TokenCandidate, bool, str, float]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="paper-task-codex-probe-") as temporary:
        root = Path(temporary)
        config_dir = root / "codex-home"
        work_dir = root / "workspace"
        config_dir.mkdir(mode=0o700)
        work_dir.mkdir(mode=0o700)
        write_config(config_dir, config, workspace=work_dir)
        env = session_environment(config, candidate.value, config_dir)
        output_path = root / "last-message.txt"
        command = [
            codex_bin,
            "--ask-for-approval",
            "never",
            "--disable",
            "plugins",
            "--disable",
            "remote_plugin",
            "--disable",
            "apps",
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--output-last-message",
            str(output_path),
            "Reply with exactly OK and nothing else.",
        ]
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=work_dir,
                env=env,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = started + timeout
            while process.poll() is None:
                if stop_event.wait(timeout=0.1):
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    return (
                        candidate,
                        False,
                        "cancelled after another token passed",
                        time.monotonic() - started,
                    )
                if time.monotonic() >= deadline:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    return (
                        candidate,
                        False,
                        f"timeout after {timeout:g}s",
                        time.monotonic() - started,
                    )
            response = ""
            if output_path.is_file():
                response = output_path.read_text(encoding="utf-8", errors="replace").strip()
            if process.returncode == 0 and response.upper() == "OK":
                success, detail = True, "success"
            elif process.returncode != 0:
                success, detail = False, f"Codex exit={process.returncode}"
            else:
                success, detail = False, "unexpected probe response"
        except FileNotFoundError:
            success, detail = False, "Codex executable not found"
        except OSError as exc:
            success, detail = False, f"unable to start Codex ({type(exc).__name__})"
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
    return candidate, success, detail, time.monotonic() - started


def select_token(
    config: dict,
    codex_bin: str,
    *,
    timeout: float = 90.0,
    workers: int = 0,
    progress: Callable[[str], None] | None = None,
) -> TokenCandidate:
    upstream = str(
        config.get("api_base_url") or codex_api_base_url(str(config["base_url"]))
    )
    with forwarding_relay(upstream) as relay:
        probe_config = dict(config)
        probe_config["api_base_url"] = relay.base_url
        return select_boyue_token(
            probe_config,
            codex_bin,
            _probe_one,
            provider_name="Codex",
            timeout=timeout,
            workers=workers,
            progress=progress,
        )


__all__ = [
    "TOKEN_ENV_KEY",
    "ForwardingRelay",
    "forwarding_relay",
    "record_token_selection",
    "relay_config_override",
    "render_config",
    "select_token",
    "session_environment",
    "start_forwarding_relay",
    "write_config",
]
