from __future__ import annotations

import os
import http.server
import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from paper_task_launcher.codex_provider import (
    TOKEN_ENV_KEY,
    render_config,
    select_token,
    session_environment,
    start_forwarding_relay,
    write_config,
)


def _loopback_available() -> bool:
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
    except OSError:
        return False
    finally:
        probe.close()
    return True


class CodexProviderTests(unittest.TestCase):
    def test_writes_private_responses_provider_without_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {
                "model": "gpt-test",
                "base_url": "https://boyue.example",
            }
            workspace = root / "task"
            workspace.mkdir()
            path = write_config(root / "codex-home", config, workspace=workspace)
            content = path.read_text(encoding="utf-8")
            self.assertIn('model = "gpt-test"', content)
            self.assertIn('wire_api = "responses"', content)
            self.assertIn(f'env_key = "{TOKEN_ENV_KEY}"', content)
            self.assertIn(f'exclude = ["{TOKEN_ENV_KEY}"]', content)
            self.assertIn("requires_openai_auth = false", content)
            self.assertIn("stream_idle_timeout_ms = 300000", content)
            self.assertIn('trust_level = "trusted"', content)
            self.assertNotIn("secret", content)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)
            with patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://proxy.invalid:8080",
                    "HTTPS_PROXY": "http://proxy.invalid:8080",
                    "ALL_PROXY": "socks5://proxy.invalid:1080",
                },
            ):
                env = session_environment(config, "secret", path.parent)
            self.assertEqual(env[TOKEN_ENV_KEY], "secret")
            self.assertEqual(env["CODEX_HOME"], str(path.parent.resolve()))
            self.assertEqual(env["CODEX_SQLITE_HOME"], str(path.parent.resolve()))
            self.assertNotIn("HTTP_PROXY", env)
            self.assertNotIn("HTTPS_PROXY", env)
            self.assertNotIn("ALL_PROXY", env)
            self.assertEqual(env["NO_PROXY"], "127.0.0.1,localhost")

    @unittest.skipUnless(_loopback_available(), "loopback sockets are unavailable")
    def test_forwarding_relay_forwards_body_errors_and_closes(self):
        received: list[tuple[str, bytes]] = []

        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                received.append((self.path, body))
                if self.path.endswith("/error"):
                    payload = b'{"error":"expected"}'
                    self.send_response(403)
                else:
                    payload = b'first-second'
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload[:5])
                self.wfile.flush()
                self.wfile.write(payload[5:])

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "NO_PROXY": "",
                "no_proxy": "",
            },
        ):
            relay = start_forwarding_relay(
                f"http://127.0.0.1:{upstream.server_port}/v1"
            )
            try:
                # Reach the loopback relay directly. The deliberately broken
                # process proxy still remains visible inside the relay thread,
                # which verifies that its upstream opener ignores that proxy.
                direct_opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({})
                )
                request = urllib.request.Request(
                    relay.base_url + "/responses",
                    data=b"request-body",
                    method="POST",
                )
                with direct_opener.open(request, timeout=5) as response:
                    self.assertEqual(response.read(), b"first-second")
                error_request = urllib.request.Request(
                    relay.base_url + "/error", data=b"error-body", method="POST"
                )
                with self.assertRaises(urllib.error.HTTPError) as captured:
                    direct_opener.open(error_request, timeout=5)
                self.assertEqual(captured.exception.code, 403)
                captured.exception.close()
                self.assertEqual(received[0], ("/v1/responses", b"request-body"))
                self.assertEqual(received[1], ("/v1/error", b"error-body"))
            finally:
                relay.close()
                upstream.shutdown()
                upstream.server_close()
                thread.join(timeout=5)
        self.assertTrue(relay._closed)

    def test_probe_is_ephemeral_and_selects_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tokens = root / "tokens.env"
            tokens.write_text("BAD=bad\nGOOD=good\n", encoding="utf-8")
            fake = root / "codex"
            fake.write_text(
                """#!/usr/bin/env python3
import os, sys
from pathlib import Path
if 'exec' not in sys.argv or '--ephemeral' not in sys.argv:
    raise SystemExit(2)
output = Path(sys.argv[sys.argv.index('--output-last-message') + 1])
output.write_text('OK' if os.environ.get('PAPER_TASK_BOYUE_TOKEN') == 'good' else 'NO')
""",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            config = {
                "mode": "boyue_token_file",
                "backend": "codex",
                "model": "gpt-test",
                "base_url": "https://boyue.example",
                "token_file": str(tokens),
            }
            @contextmanager
            def fake_relay(_upstream: str):
                yield SimpleNamespace(base_url="http://127.0.0.1:12345/v1")

            with patch(
                "paper_task_launcher.codex_provider.forwarding_relay",
                side_effect=fake_relay,
            ):
                selected = select_token(config, str(fake), progress=None)
            self.assertEqual(selected.label, "GOOD")


if __name__ == "__main__":
    unittest.main()
