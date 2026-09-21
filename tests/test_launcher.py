from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from paper_task_launcher.boyue_provider import TokenCandidate
from paper_task_launcher.errors import LauncherError
from paper_task_launcher.exporter import export_dataset
from paper_task_launcher.git_history import SnapshotStore
from paper_task_launcher.launcher import (
    launch_task,
    prepare_continuation_task,
    prepare_task,
    prepare_web_task,
    resume_task,
    validate_continuation_workspace,
    validate_empty_workspace,
    validate_web_source,
)
from paper_task_launcher.util import run_checked


class LauncherTests(unittest.TestCase):
    def test_kimi_web_copy_launch_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "web"
            source.mkdir()
            (source / "index.html").write_text("baseline\n", encoding="utf-8")
            paper = root / "paper.pdf"
            paper.write_bytes(b"%PDF-1.4\n")
            tokens = root / "tokens.env"
            tokens.write_text("PRIMARY=good-secret\n", encoding="utf-8")
            fake = root / "kimi"
            fake.write_text(
                """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
if '--prompt' in args:
    print('OK')
    raise SystemExit(0)

home = Path(os.environ['KIMI_CODE_HOME'])
cwd = Path.cwd()
if '--session' in args:
    session_id = args[args.index('--session') + 1]
else:
    session_id = 'session_fake-kimi'
session_dir = home / 'sessions' / 'wd_test' / session_id
wire = session_dir / 'agents' / 'main' / 'wire.jsonl'
wire.parent.mkdir(parents=True, exist_ok=True)
state = {
    'id': session_id,
    'version': 1,
    'cwd': str(cwd),
    'createdAt': 1000,
    'updatedAt': 2000,
}
(session_dir / 'state.json').write_text(json.dumps(state))
turn = 0
if wire.exists():
    turn = sum(1 for line in wire.read_text().splitlines()
               if json.loads(line).get('type') == 'turn.ended')
prompt = f'kimi request {turn + 1}'
response = f'kimi response {turn + 1}'
with (cwd / 'index.html').open('a') as handle:
    handle.write(f'changed-{turn + 1}\\n')
records = [
    {'type': 'turn.prompt', 'agentId': 'main',
     'input': [{'type': 'text', 'text': prompt}], 'time': 1000 + turn},
    {'type': 'context.append_loop_event', 'event': {
        'type': 'content.part', 'turnId': turn, 'step': 1,
        'part': {'type': 'text', 'text': response}}},
    {'type': 'context.append_loop_event', 'event': {
        'type': 'step.end', 'turnId': turn, 'step': 1,
        'finishReason': 'end_turn'}},
    {'type': 'turn.ended', 'agentId': 'main', 'turnId': turn,
     'reason': 'completed', 'durationMs': 10, 'time': 2000 + turn},
]
with wire.open('a') as handle:
    for record in records:
        handle.write(json.dumps(record) + '\\n')
""",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            workspace = root / "task"

            self.assertEqual(
                launch_task(
                    str(workspace),
                    str(paper),
                    backend="kimi",
                    kimi_bin=str(fake),
                    continue_existing=True,
                    web_source=str(source),
                    model="kimi-k3",
                    token_file=str(tokens),
                    boyue_url="http://boyue.example",
                    progress=None,
                ),
                0,
            )
            self.assertEqual(
                resume_task(str(workspace), kimi_bin=str(fake), progress=None), 0
            )
            turns = [
                json.loads(line)
                for line in (workspace / ".recording" / "transcript.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertEqual(
                [turn["user_input"] for turn in turns],
                ["kimi request 1", "kimi request 2"],
            )
            self.assertEqual(
                [turn["final_response"] for turn in turns],
                ["kimi response 1", "kimi response 2"],
            )
            self.assertEqual([turn["model"] for turn in turns], ["kimi-k3"] * 2)
            manifest = json.loads(
                (workspace / ".recording" / "manifest.json").read_text()
            )
            self.assertEqual(manifest["backend"], "kimi")
            self.assertEqual(manifest["turn_count"], 2)
            self.assertEqual(manifest["resume_count"], 1)
            config_text = (
                workspace / ".recording" / "kimi-home" / "config.toml"
            ).read_text()
            self.assertNotIn("good-secret", config_text)
            self.assertIn("api_key_env", config_text)

            exported = export_dataset(str(workspace), progress=None)
            dataset = json.loads((exported / "dataset.json").read_text())
            self.assertEqual(dataset["backend"], "kimi")
            self.assertEqual(dataset["modification_model"], "kimi-k3")
            self.assertTrue((exported / "versions" / "baseline" / "index.html").is_file())
            self.assertTrue((exported / "versions" / "turn-0002" / "index.html").is_file())
            exported_manifest = json.loads(
                (exported / "recording-manifest.json").read_text()
            )
            self.assertNotIn("token_file", exported_manifest["boyue_provider"])
            exported_bytes = b"".join(
                path.read_bytes() for path in exported.rglob("*") if path.is_file()
            )
            self.assertNotIn(b"good-secret", exported_bytes)
            self.assertNotIn(b"kimi-home", exported_bytes)

    def test_web_uses_project_token_pool_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "web"
            source.mkdir()
            (source / "index.html").write_text("baseline\n", encoding="utf-8")
            paper = root / "paper.pdf"
            paper.write_bytes(b"%PDF-1.4\n")
            tokens = root / ".env.token"
            tokens.write_text("PRIMARY=not-probed\n", encoding="utf-8")
            workspace = root / "task"

            with patch(
                "paper_task_launcher.launcher.DEFAULT_TOKEN_FILE", tokens
            ):
                result = launch_task(
                    str(workspace),
                    str(paper),
                    backend="codex",
                    continue_existing=True,
                    web_source=str(source),
                    model="gpt-test",
                    prepare_only=True,
                    progress=None,
                )

            self.assertEqual(result, 0)
            manifest = json.loads(
                (workspace / ".recording" / "manifest.json").read_text()
            )
            self.assertEqual(
                manifest["boyue_provider"]["token_file"], str(tokens.resolve())
            )

    def test_web_source_resource_and_generated_model_are_optional(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "web"
            source.mkdir()
            validated = validate_web_source(str(source), str(root / "task"))
            self.assertIsNone(validated[1])
            (source / "resource.json").write_text("{}", encoding="utf-8")
            validated = validate_web_source(str(source), str(root / "task"))
            self.assertIsNone(validated[1])
            (source / "resource.json").write_text("not-json", encoding="utf-8")
            validated = validate_web_source(str(source), str(root / "task"))
            self.assertIsNone(validated[1])

    def test_web_prepare_only_validates_without_token_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "web"
            source.mkdir()
            (source / "resource.json").write_text("{}\n", encoding="utf-8")
            (source / "index.html").write_text("baseline\n", encoding="utf-8")
            paper = root / "paper.pdf"
            paper.write_bytes(b"%PDF-1.4\n")
            tokens = root / "tokens.env"
            tokens.write_text("PRIMARY=not-probed\n", encoding="utf-8")
            workspace = root / "task"
            with patch(
                "paper_task_launcher.launcher.select_codex_token",
                side_effect=AssertionError("prepare-only must not probe"),
            ):
                code = launch_task(
                    str(workspace),
                    str(paper),
                    backend="codex",
                    model="gpt-user-choice",
                    codex_bin="missing-is-allowed-for-prepare-only",
                    web_source=str(source),
                    token_file=str(tokens),
                    prepare_only=True,
                    progress=None,
                )
            self.assertEqual(code, 0)
            manifest = json.loads(
                (workspace / ".recording" / "manifest.json").read_text()
            )
            self.assertIsNone(manifest["generated_model"])
            self.assertEqual(manifest["modification_model"], "gpt-user-choice")
            self.assertEqual(
                manifest["boyue_provider"]["base_url"],
                "http://35.220.164.252:3888",
            )
            self.assertEqual(
                manifest["boyue_provider"]["api_base_url"],
                "http://35.220.164.252:3888/v1",
            )
            self.assertTrue(
                (workspace / ".recording" / "codex-home" / "config.toml").is_file()
            )
            self.assertNotIn(
                "last_selected_token_label", manifest["boyue_provider"]
            )

    def test_web_rejects_model_override_before_creating_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "web"
            source.mkdir()
            (source / "resource.json").write_text(
                '{"generated_model":"source-model"}\n', encoding="utf-8"
            )
            paper = root / "paper.pdf"
            paper.write_bytes(b"%PDF-1.4\n")
            tokens = root / "tokens.env"
            tokens.write_text("PRIMARY=token\n", encoding="utf-8")
            workspace = root / "task"
            with self.assertRaisesRegex(LauncherError, "cannot be used with --web"):
                launch_task(
                    str(workspace),
                    str(paper),
                    backend="claude",
                    web_source=str(source),
                    model="user-model",
                    claude_model="override",
                    token_file=str(tokens),
                    boyue_url="https://boyue.example",
                    prepare_only=True,
                    progress=None,
                )
            self.assertFalse(workspace.exists())

    def test_web_requires_user_selected_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "web"
            source.mkdir()
            (source / "resource.json").write_text("{}\n", encoding="utf-8")
            paper = root / "paper.pdf"
            paper.write_bytes(b"%PDF-1.4\n")
            workspace = root / "task"
            with self.assertRaisesRegex(LauncherError, "non-empty --model"):
                launch_task(
                    str(workspace),
                    str(paper),
                    backend="codex",
                    web_source=str(source),
                    token_file=str(root / "tokens.env"),
                    boyue_url="https://boyue.example",
                    prepare_only=True,
                    progress=None,
                )
            self.assertFalse(workspace.exists())

    def test_prepare_web_task_copies_safe_complete_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "web"
            source.mkdir()
            (source / "resource.json").write_text(
                '{"generated_model":"kimi-k3"}\n', encoding="utf-8"
            )
            (source / ".gitignore").write_text("dist/\n", encoding="utf-8")
            (source / "index.html").write_text("before\n", encoding="utf-8")
            (source / "dist").mkdir()
            (source / "dist" / "bundle.js").write_text("built\n", encoding="utf-8")
            (source / "node_modules").mkdir()
            (source / "node_modules" / "package.js").write_text("dependency\n")
            (source / ".env").write_text("SECRET=do-not-copy\n")
            paper = root / "paper.pdf"
            paper.write_bytes(b"%PDF-1.4\nweb task\n")
            workspace = root / "task"
            validated = validate_web_source(str(source), str(workspace))
            task = prepare_web_task(
                str(workspace),
                str(paper),
                validated[0],
                validated[1],
                "user-selected-model",
                backend="claude",
                progress=None,
            )

            self.assertEqual(task.prompt, "")
            self.assertEqual(task.manifest["task_mode"], "web_copy")
            self.assertEqual(task.manifest["generated_model"], "kimi-k3")
            self.assertEqual(task.manifest["modification_model"], "user-selected-model")
            self.assertEqual(task.manifest["snapshot_policy"], "web")
            self.assertTrue((source / "index.html").is_file())
            self.assertFalse((workspace / "node_modules").exists())
            self.assertFalse((workspace / ".env").exists())
            baseline = task.manifest["baseline_snapshot"]["commit"]
            self.assertEqual(
                run_checked(["git", "show", f"{baseline}:dist/bundle.js"], cwd=workspace),
                "built",
            )
            self.assertEqual(
                run_checked(["git", "show", f"{baseline}:resource.json"], cwd=workspace),
                '{"generated_model":"kimi-k3"}',
            )

    def test_codex_web_copy_launch_resume_and_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "web"
            source.mkdir()
            (source / "resource.json").write_text(
                '{"generated_model":"original-generator"}\n', encoding="utf-8"
            )
            (source / "index.html").write_text("baseline\n", encoding="utf-8")
            paper = root / "paper.pdf"
            paper.write_bytes(b"%PDF-1.4\nweb task\n")
            tokens = root / "tokens.env"
            tokens.write_text("PRIMARY=working-secret\n", encoding="utf-8")
            workspace = root / "task"
            fake = root / "codex"
            fake.write_text(
                """#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
workspace = Path(sys.argv[sys.argv.index('--cd') + 1]).resolve()
config_home = Path(os.environ['CODEX_HOME']).resolve()
if config_home != workspace / '.recording' / 'codex-home':
    raise SystemExit('wrong CODEX_HOME')
if os.environ.get('PAPER_TASK_BOYUE_TOKEN') != 'working-secret':
    raise SystemExit('wrong token')
if sys.argv[sys.argv.index('--model') + 1] != 'gpt-boyue-test':
    raise SystemExit('wrong model')
resuming = 'resume' in sys.argv
expected = 9 if resuming else 7
if len(sys.argv) != expected:
    raise SystemExit('unexpected prompt or arguments: ' + repr(sys.argv))
log = config_home / 'sessions' / '2026' / '01' / '01' / 'rollout-web.jsonl'
log.parent.mkdir(parents=True, exist_ok=True)
turn = 'turn-2' if resuming else 'turn-1'
records = []
if not resuming:
    records.append({'timestamp':'2026-01-01T00:00:00Z','type':'session_meta','payload':{'session_id':'web-session','cwd':str(workspace),'cli_version':'test'}})
records.extend([
  {'timestamp':'2026-01-01T00:00:01Z','type':'event_msg','payload':{'type':'task_started','turn_id':turn}},
  {'timestamp':'2026-01-01T00:00:02Z','type':'event_msg','payload':{'type':'user_message','message':('second change' if resuming else 'first change')}},
])
(workspace / 'index.html').write_text('v2\\n' if resuming else 'v1\\n')
records.append({'timestamp':'2026-01-01T00:00:03Z','type':'event_msg','payload':{'type':'task_complete','turn_id':turn,'last_agent_message':('second done' if resuming else 'first done')}})
with log.open('a') as handle:
    for record in records:
        handle.write(json.dumps(record) + '\\n')
        handle.flush()
time.sleep(0.4)
""",
                encoding="utf-8",
            )
            fake.chmod(0o755)

            @contextmanager
            def fake_relay(_upstream: str):
                yield SimpleNamespace(base_url="http://127.0.0.1:12345/v1")

            selected = TokenCandidate("PRIMARY", "working-secret", 0)
            with patch(
                "paper_task_launcher.launcher.select_codex_token",
                return_value=selected,
            ), patch(
                "paper_task_launcher.launcher.codex_forwarding_relay",
                side_effect=fake_relay,
            ):
                code = launch_task(
                    str(workspace),
                    str(paper),
                    backend="codex",
                    codex_bin=str(fake),
                    continue_existing=True,
                    web_source=str(source),
                    model="gpt-boyue-test",
                    token_file=str(tokens),
                    boyue_url="https://boyue.example",
                    progress=None,
                )
                self.assertEqual(code, 0)
                self.assertEqual((source / "index.html").read_text(), "baseline\n")
                code = resume_task(str(workspace), codex_bin=str(fake), progress=None)
                self.assertEqual(code, 0)

            turns = [
                json.loads(line)
                for line in (workspace / ".recording" / "transcript.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertEqual([turn["model"] for turn in turns], ["gpt-boyue-test"] * 2)
            self.assertEqual([turn["user_input"] for turn in turns], ["first change", "second change"])
            manifest = json.loads(
                (workspace / ".recording" / "manifest.json").read_text()
            )
            self.assertEqual(manifest["turn_count"], 2)
            self.assertTrue((workspace / ".recording" / "codex-home" / "config.toml").is_file())
            self.assertNotIn(
                "working-secret",
                (workspace / ".recording" / "codex-home" / "config.toml").read_text(),
            )

            output = export_dataset(str(workspace), str(root / "export"), progress=None)
            dataset = json.loads((output / "dataset.json").read_text())
            self.assertEqual(dataset["generated_model"], "original-generator")
            self.assertEqual(dataset["modification_model"], "gpt-boyue-test")
            self.assertEqual(
                (output / "versions" / "baseline" / "index.html").read_text(),
                "baseline\n",
            )
            self.assertEqual(
                (output / "versions" / "turn-0002" / "index.html").read_text(),
                "v2\n",
            )
            exported_manifest = json.loads(
                (output / "recording-manifest.json").read_text()
            )
            self.assertNotIn("workspace", exported_manifest)
            self.assertNotIn("token_file", exported_manifest["boyue_provider"])
            self.assertNotIn("source_path", exported_manifest["input_web"])
            self.assertFalse((output / ".recording").exists())
            exported_bytes = b"".join(
                path.read_bytes() for path in output.rglob("*") if path.is_file()
            )
            self.assertNotIn(b"working-secret", exported_bytes)
            self.assertNotIn(str(tokens.resolve()).encode(), exported_bytes)
            self.assertNotIn(str(source.resolve()).encode(), exported_bytes)

            Path(manifest["session_log"]).unlink()
            with patch(
                "paper_task_launcher.launcher.select_codex_token",
                return_value=selected,
            ), self.assertRaisesRegex(LauncherError, "session state is missing"):
                resume_task(str(workspace), codex_bin=str(fake), progress=None)
            unchanged = json.loads(
                (workspace / ".recording" / "manifest.json").read_text()
            )
            self.assertEqual(unchanged["state"], "completed")
            self.assertEqual(unchanged["resume_count"], 1)

    def test_claude_web_copy_uses_user_selected_model_without_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "web"
            source.mkdir()
            (source / "resource.json").write_text(
                '{"generated_model":"original-generator"}\n', encoding="utf-8"
            )
            (source / "index.html").write_text("baseline\n", encoding="utf-8")
            paper = root / "paper.pdf"
            paper.write_bytes(b"%PDF-1.4\nweb task\n")
            tokens = root / "tokens.env"
            tokens.write_text("PRIMARY=claude-secret\n", encoding="utf-8")
            workspace = root / "task"
            fake = root / "claude"
            fake.write_text(
                """#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
if '-p' in sys.argv:
    print(json.dumps({'result':'OK' if os.environ.get('ANTHROPIC_AUTH_TOKEN') == 'claude-secret' else 'NO'}))
    raise SystemExit(0)
settings_path = Path(sys.argv[sys.argv.index('--settings') + 1]).resolve()
workspace = Path.cwd()
if settings_path.parent != workspace / '.recording' / 'claude-config':
    raise SystemExit('wrong config directory')
if sys.argv[sys.argv.index('--model') + 1] != 'kimi-web-test':
    raise SystemExit('wrong model')
resuming = '--resume' in sys.argv
if (not resuming and len(sys.argv) != 5) or (resuming and len(sys.argv) != 7):
    raise SystemExit('unexpected prompt or arguments: ' + repr(sys.argv))
settings = json.loads(settings_path.read_text())
common = {'session_id':'claude-web-session','transcript_path':str(settings_path.parent / 'session.jsonl'),'cwd':str(workspace)}
(settings_path.parent / 'session.jsonl').touch()
prompt_id = 'web-2' if resuming else 'web-1'
prompt = 'second claude change' if resuming else 'first claude change'
for event in [
  {**common,'hook_event_name':'SessionStart','model':'kimi-web-test'},
  {**common,'hook_event_name':'UserPromptSubmit','prompt_id':prompt_id,'prompt':prompt},
]:
    hook = settings['hooks'][event['hook_event_name']][0]['hooks'][0]
    subprocess.run([hook['command'], *hook['args']], input=json.dumps(event), text=True, check=True)
(workspace / 'index.html').write_text('claude-v2\\n' if resuming else 'claude-v1\\n')
stop = {**common,'hook_event_name':'Stop','prompt_id':prompt_id,'last_assistant_message':'done','stop_hook_active':False}
hook = settings['hooks']['Stop'][0]['hooks'][0]
subprocess.run([hook['command'], *hook['args']], input=json.dumps(stop), text=True, check=True)
""",
                encoding="utf-8",
            )
            fake.chmod(0o755)

            self.assertEqual(
                launch_task(
                    str(workspace),
                    str(paper),
                    backend="claude",
                    claude_bin=str(fake),
                    web_source=str(source),
                    model="kimi-web-test",
                    token_file=str(tokens),
                    boyue_url="https://boyue.example",
                    progress=None,
                ),
                0,
            )
            self.assertEqual(
                resume_task(str(workspace), claude_bin=str(fake), progress=None), 0
            )
            turns = [
                json.loads(line)
                for line in (workspace / ".recording" / "transcript.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertEqual(
                [turn["user_input"] for turn in turns],
                ["first claude change", "second claude change"],
            )
            self.assertEqual([turn["model"] for turn in turns], ["kimi-web-test"] * 2)
            manifest = json.loads(
                (workspace / ".recording" / "manifest.json").read_text()
            )
            self.assertEqual(manifest["boyue_provider"]["backend"], "claude")
            self.assertEqual(manifest["generated_model"], "original-generator")
            self.assertEqual(manifest["modification_model"], "kimi-web-test")
            self.assertEqual(manifest["turn_count"], 2)
    def test_rejects_nonempty_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "task"
            workspace.mkdir()
            (workspace / "file").write_text("x", encoding="utf-8")
            with self.assertRaises(LauncherError):
                validate_empty_workspace(str(workspace))

    def test_prepare_and_snapshot_local_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = root / "paper"
            paper.mkdir()
            (paper / "main.tex").write_text("paper", encoding="utf-8")
            workspace = root / "task"
            progress = []
            task = prepare_task(str(workspace), str(paper), progress=progress.append)
            self.assertTrue((workspace / ".git").is_dir())
            self.assertTrue((workspace / "paper-source" / "main.tex").exists())
            self.assertIn("/paper-source/", (workspace / ".gitignore").read_text())
            self.assertIn("/out_data/", (workspace / ".gitignore").read_text())
            (workspace / "implementation.py").write_text("print('ok')\n", encoding="utf-8")
            store = SnapshotStore(workspace, task.recording_id)
            snapshot = store.capture("test")
            self.assertTrue(snapshot["commit"])
            self.assertTrue(any("论文源码导入完成" in message for message in progress))
            self.assertTrue(any("基线快照" in message for message in progress))

    def test_failed_preparation_cleans_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "task"
            with patch(
                "paper_task_launcher.launcher.import_paper",
                side_effect=LauncherError("network failed"),
            ):
                with self.assertRaises(LauncherError):
                    prepare_task(str(workspace), "2608.15089")
            self.assertTrue(workspace.is_dir())
            self.assertEqual(list(workspace.iterdir()), [])

    def test_prepare_continuation_captures_existing_web_as_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = root / "paper.pdf"
            paper.write_bytes(b"%PDF-1.4\nexisting web paper\n")
            workspace = root / "existing-web"
            workspace.mkdir()
            (workspace / "index.html").write_text("before\n", encoding="utf-8")

            task = prepare_continuation_task(
                str(workspace), str(paper), progress=None
            )

            self.assertEqual(task.prompt, "")
            self.assertEqual(task.manifest["task_mode"], "continue")
            self.assertEqual(task.manifest["paper"]["kind"], "local_pdf")
            self.assertIsNone(task.manifest["initial_prompt_path"])
            self.assertFalse((workspace / ".recording" / "initial-prompt.md").exists())
            self.assertEqual(
                (workspace / "paper-source" / "paper.pdf").read_bytes(),
                paper.read_bytes(),
            )
            baseline = task.manifest["baseline_snapshot"]["commit"]
            self.assertEqual(
                run_checked(["git", "show", f"{baseline}:index.html"], cwd=workspace),
                "before",
            )
            self.assertEqual(run_checked(["git", "rev-parse", "HEAD"], cwd=workspace), baseline)
            self.assertEqual(run_checked(["git", "status", "--porcelain"], cwd=workspace), "")
            self.assertIn(
                "/.recording/",
                (workspace / ".git" / "info" / "exclude").read_text(),
            )
            self.assertIn(
                "/paper-source/",
                (workspace / ".git" / "info" / "exclude").read_text(),
            )

            with self.assertRaisesRegex(LauncherError, "paper-task resume"):
                validate_continuation_workspace(str(workspace))

    def test_continue_requires_paper_without_touching_existing_web(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "existing-web"
            workspace.mkdir()
            (workspace / "index.html").write_text("unchanged\n", encoding="utf-8")

            with self.assertRaisesRegex(LauncherError, "--paper is required"):
                launch_task(
                    str(workspace),
                    None,
                    continue_existing=True,
                    prepare_only=True,
                    progress=None,
                )

            self.assertEqual(
                (workspace / "index.html").read_text(encoding="utf-8"),
                "unchanged\n",
            )
            self.assertFalse((workspace / ".git").exists())
            self.assertFalse((workspace / ".recording").exists())

    def test_prepare_continuation_preserves_existing_git_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = root / "paper"
            paper.mkdir()
            (paper / "main.tex").write_text("paper\n", encoding="utf-8")
            workspace = root / "existing-repository"
            workspace.mkdir()
            run_checked(["git", "init", "-b", "main", str(workspace)])
            (workspace / "index.html").write_text("committed\n", encoding="utf-8")
            run_checked(["git", "add", "index.html"], cwd=workspace)
            run_checked(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-m",
                    "existing web",
                ],
                cwd=workspace,
            )
            (workspace / "index.html").write_text("working tree version\n", encoding="utf-8")
            head_before = run_checked(["git", "rev-parse", "HEAD"], cwd=workspace)
            status_before = run_checked(["git", "status", "--porcelain"], cwd=workspace)

            task = prepare_continuation_task(
                str(workspace), str(paper), progress=None
            )

            self.assertTrue(task.manifest["continued_workspace_had_git"])
            self.assertEqual(
                run_checked(["git", "rev-parse", "HEAD"], cwd=workspace), head_before
            )
            self.assertEqual(
                run_checked(["git", "status", "--porcelain"], cwd=workspace),
                status_before,
            )
            baseline = task.manifest["baseline_snapshot"]["commit"]
            self.assertEqual(
                run_checked(["git", "show", f"{baseline}:index.html"], cwd=workspace),
                "working tree version",
            )

    def test_full_continuation_launch_does_not_send_generation_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = root / "paper"
            paper.mkdir()
            (paper / "main.tex").write_text("paper\n", encoding="utf-8")
            workspace = root / "existing-web"
            workspace.mkdir()
            (workspace / "index.html").write_text("before\n", encoding="utf-8")
            codex_home = root / "codex-home"
            fake = root / "fake-codex"
            fake.write_text(
                """#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
workspace = Path(sys.argv[sys.argv.index('--cd') + 1]).resolve()
if sys.argv != [sys.argv[0], '--cd', str(workspace)]:
    raise SystemExit('unexpected prompt or arguments: ' + repr(sys.argv))
(workspace / 'index.html').write_text('after\\n')
log = Path(os.environ['CODEX_HOME']) / 'sessions' / '2026' / '01' / '01' / 'rollout-continue.jsonl'
log.parent.mkdir(parents=True, exist_ok=True)
records = [
  {'timestamp':'2026-01-01T00:00:00Z','type':'session_meta','payload':{'session_id':'session-continue','cwd':str(workspace),'cli_version':'test'}},
  {'timestamp':'2026-01-01T00:00:01Z','type':'event_msg','payload':{'type':'task_started','turn_id':'turn-continue'}},
  {'timestamp':'2026-01-01T00:00:02Z','type':'event_msg','payload':{'type':'user_message','message':'change the existing web'}},
  {'timestamp':'2026-01-01T00:00:03Z','type':'event_msg','payload':{'type':'task_complete','turn_id':'turn-continue','last_agent_message':'changed','completed_at':'2026-01-01T00:00:03Z'}},
]
with log.open('w') as handle:
    for record in records:
        handle.write(json.dumps(record) + '\\n')
        handle.flush()
time.sleep(0.4)
""",
                encoding="utf-8",
            )
            fake.chmod(0o755)

            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                code = launch_task(
                    str(workspace),
                    str(paper),
                    continue_existing=True,
                    codex_bin=str(fake),
                    progress=None,
                )

            self.assertEqual(code, 0)
            transcript = json.loads(
                (workspace / ".recording" / "transcript.jsonl").read_text()
            )
            self.assertEqual(transcript["user_input"], "change the existing web")
            self.assertEqual(transcript["final_response"], "changed")
            baseline = json.loads(
                (workspace / ".recording" / "manifest.json").read_text()
            )["baseline_snapshot"]["commit"]
            self.assertEqual(
                run_checked(["git", "show", f"{baseline}:index.html"], cwd=workspace),
                "before",
            )
            self.assertEqual(
                run_checked(
                    ["git", "show", f"{transcript['snapshot']['commit']}:index.html"],
                    cwd=workspace,
                ),
                "after",
            )

            output = export_dataset(
                str(workspace), str(root / "exported"), progress=None
            )
            dataset = json.loads((output / "dataset.json").read_text())
            self.assertEqual(dataset["paper"]["kind"], "local_latex_directory")
            self.assertIsNone(dataset["paths"]["initial_prompt"])
            self.assertEqual(dataset["paths"]["paper_source"], "paper-source")
            self.assertEqual(
                (output / "paper-source" / "main.tex").read_text(), "paper\n"
            )
            self.assertEqual(
                (output / "versions" / "baseline" / "index.html").read_text(),
                "before\n",
            )
            self.assertEqual(
                (output / "versions" / "turn-0001" / "index.html").read_text(),
                "after\n",
            )

    def test_full_launch_with_fake_codex_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = root / "paper"
            paper.mkdir()
            (paper / "main.tex").write_text("paper", encoding="utf-8")
            workspace = root / "task"
            codex_home = root / "codex-home"
            fake = root / "fake-codex"
            fake.write_text(
                """#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
workspace = Path(sys.argv[sys.argv.index('--cd') + 1]).resolve()
log = Path(os.environ['CODEX_HOME']) / 'sessions' / '2026' / '01' / '01' / 'rollout-test.jsonl'
log.parent.mkdir(parents=True, exist_ok=True)
if len(sys.argv) > 1 and sys.argv[1] == 'resume':
    records = [
      {'timestamp':'2026-01-01T00:01:01Z','ordinal':4,'type':'event_msg','payload':{'type':'task_started','turn_id':'turn-resume'}},
      {'timestamp':'2026-01-01T00:01:02Z','ordinal':5,'type':'event_msg','payload':{'type':'item_completed','turn_id':'turn-resume','item':{'type':'UserMessage','content':[{'type':'text','text':'follow up'}]}}},
      {'timestamp':'2026-01-01T00:01:03Z','ordinal':6,'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn-resume','last_agent_message':'resumed done','completed_at':'2026-01-01T00:01:03Z'}},
    ]
    mode = 'a'
    (workspace / 'resumed.py').write_text("print('resumed')\\n")
else:
    prompt = sys.argv[-1]
    records = [
      {'timestamp':'2026-01-01T00:00:00Z','ordinal':0,'type':'session_meta','payload':{'session_id':'session-test','cwd':str(workspace),'cli_version':'test'}},
      {'timestamp':'2026-01-01T00:00:01Z','ordinal':1,'type':'event_msg','payload':{'type':'task_started','turn_id':'turn-test'}},
      {'timestamp':'2026-01-01T00:00:02Z','ordinal':2,'type':'event_msg','payload':{'type':'item_completed','turn_id':'turn-test','item':{'type':'UserMessage','content':[{'type':'text','text':prompt}]}}},
      {'timestamp':'2026-01-01T00:00:03Z','ordinal':3,'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn-test','last_agent_message':'done','completed_at':'2026-01-01T00:00:03Z'}},
    ]
    mode = 'w'
with log.open(mode) as handle:
    for record in records:
        handle.write(json.dumps(record) + '\\n')
        handle.flush()
time.sleep(0.4)
""",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                code = launch_task(
                    str(workspace), str(paper), codex_bin=str(fake), progress=None
                )
            self.assertEqual(code, 0)
            transcript = [
                json.loads(line)
                for line in (workspace / ".recording" / "transcript.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(transcript), 1)
            self.assertEqual(transcript[0]["final_response"], "done")
            self.assertIn("论文文件", transcript[0]["user_input"])
            self.assertIn("前端网页", transcript[0]["user_input"])
            self.assertIn("对用户友好", transcript[0]["user_input"])
            self.assertIn("高覆盖率", transcript[0]["user_input"])
            self.assertIn("尽量不遗漏论文中的内容与细节", transcript[0]["user_input"])
            self.assertIn("渐进信息展现方式", transcript[0]["user_input"])
            self.assertIn("项目规则", transcript[0]["user_input"])
            self.assertNotIn("复现", transcript[0]["user_input"])
            self.assertNotIn("术语表", transcript[0]["user_input"])
            self.assertNotIn("响应式布局", transcript[0]["user_input"])
            manifest = json.loads((workspace / ".recording" / "manifest.json").read_text())
            self.assertEqual(manifest["session_id"], "session-test")
            self.assertEqual(manifest["turn_count"], 1)
            self.assertEqual(manifest["state"], "completed")

            # Older recordings have no saved log offset. Resume must safely rescan
            # their log without duplicating the already recorded turn.
            manifest.pop("session_log_offset", None)
            (workspace / ".recording" / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                code = resume_task(str(workspace), codex_bin=str(fake), progress=None)
            self.assertEqual(code, 0)
            transcript = [
                json.loads(line)
                for line in (workspace / ".recording" / "transcript.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(transcript), 2)
            self.assertEqual(transcript[1]["turn_index"], 2)
            self.assertEqual(transcript[1]["user_input"], "follow up")
            self.assertEqual(transcript[1]["final_response"], "resumed done")
            self.assertEqual(
                transcript[1]["snapshot"]["index"], 2
            )
            self.assertEqual(
                run_checked(
                    ["git", "rev-parse", f"{transcript[1]['snapshot']['commit']}^"],
                    cwd=workspace,
                ),
                transcript[0]["snapshot"]["commit"],
            )
            manifest = json.loads((workspace / ".recording" / "manifest.json").read_text())
            self.assertEqual(manifest["turn_count"], 2)
            self.assertEqual(manifest["resume_count"], 1)
            self.assertEqual(manifest["resume_history"][0]["turn_count_before"], 1)
            self.assertEqual(manifest["resume_history"][0]["turn_count_after"], 2)

    def test_full_launch_with_fake_claude_hooks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = root / "paper"
            paper.mkdir()
            (paper / "main.tex").write_text("paper", encoding="utf-8")
            workspace = root / "task"
            fake = root / "fake-claude"
            fake.write_text(
                """#!/usr/bin/env python3
import json, subprocess, sys
from pathlib import Path
settings_path = Path(sys.argv[sys.argv.index('--settings') + 1])
settings = json.loads(settings_path.read_text())
workspace = Path.cwd()
common = {'session_id':'claude-session','transcript_path':'/tmp/claude.jsonl','cwd':str(workspace)}
resuming = '--resume' in sys.argv
prompt = 'claude follow up' if resuming else sys.argv[-1]
prompt_id = 'prompt-2' if resuming else 'prompt-1'
events = [
  {**common,'hook_event_name':'SessionStart','model':'claude-test'},
  {**common,'hook_event_name':'UserPromptSubmit','prompt_id':prompt_id,'prompt':prompt},
]
for event in events:
    hook = settings['hooks'][event['hook_event_name']][0]['hooks'][0]
    subprocess.run([hook['command'], *hook['args']], input=json.dumps(event), text=True, check=True)
(workspace / ('claude-resumed.py' if resuming else 'claude-app.py')).write_text("print('ok')\\n")
stop = {**common,'hook_event_name':'Stop','prompt_id':prompt_id,'last_assistant_message':('claude resumed' if resuming else 'claude done'),'stop_hook_active':False}
hook = settings['hooks']['Stop'][0]['hooks'][0]
subprocess.run([hook['command'], *hook['args']], input=json.dumps(stop), text=True, check=True)
""",
                encoding="utf-8",
            )
            fake.chmod(0o755)

            code = launch_task(
                str(workspace),
                str(paper),
                backend="claude",
                claude_bin=str(fake),
                progress=None,
            )

            self.assertEqual(code, 0)
            transcript = json.loads(
                (workspace / ".recording" / "transcript.jsonl").read_text()
            )
            self.assertIn("论文文件", transcript["user_input"])
            self.assertEqual(transcript["final_response"], "claude done")
            manifest = json.loads(
                (workspace / ".recording" / "manifest.json").read_text()
            )
            self.assertEqual(manifest["backend"], "claude")
            self.assertEqual(manifest["session_id"], "claude-session")
            self.assertEqual(manifest["turn_count"], 1)
            self.assertEqual(manifest["state"], "completed")

            code = resume_task(
                str(workspace), claude_bin=str(fake), progress=None
            )
            self.assertEqual(code, 0)
            transcript = [
                json.loads(line)
                for line in (workspace / ".recording" / "transcript.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(transcript), 2)
            self.assertEqual(transcript[1]["user_input"], "claude follow up")
            self.assertEqual(transcript[1]["final_response"], "claude resumed")
            manifest = json.loads(
                (workspace / ".recording" / "manifest.json").read_text()
            )
            self.assertEqual(manifest["turn_count"], 2)
            self.assertEqual(manifest["resume_count"], 1)

    def test_managed_claude_launch_and_resume_retest_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = root / "paper"
            paper.mkdir()
            (paper / "main.tex").write_text("paper", encoding="utf-8")
            token_file = root / "tokens.env"
            token_file.write_text(
                "BROKEN=bad-secret\nWORKING=good-secret\n", encoding="utf-8"
            )
            workspace = root / "task"
            fake = root / "fake-claude"
            fake.write_text(
                """#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
if '-p' in sys.argv:
    token = os.environ.get('ANTHROPIC_AUTH_TOKEN')
    print(json.dumps({'result': 'OK' if token == 'good-secret' else 'NO'}))
    raise SystemExit(0)
settings_path = Path(sys.argv[sys.argv.index('--settings') + 1]).resolve()
if os.environ.get('CLAUDE_CONFIG_DIR') != str(settings_path.parent):
    raise SystemExit('wrong config directory')
if os.environ.get('ANTHROPIC_AUTH_TOKEN') != 'good-secret':
    raise SystemExit('wrong token')
if sys.argv[sys.argv.index('--model') + 1] != 'kimi-k3':
    raise SystemExit('wrong model')
workspace = Path.cwd()
settings = json.loads(settings_path.read_text())
common = {'session_id':'managed-session','transcript_path':'/tmp/managed.jsonl','cwd':str(workspace)}
resuming = '--resume' in sys.argv
prompt = 'managed follow up' if resuming else sys.argv[-1]
prompt_id = 'managed-2' if resuming else 'managed-1'
events = [
  {**common,'hook_event_name':'SessionStart','model':'kimi-k3'},
  {**common,'hook_event_name':'UserPromptSubmit','prompt_id':prompt_id,'prompt':prompt},
]
for event in events:
    hook = settings['hooks'][event['hook_event_name']][0]['hooks'][0]
    subprocess.run([hook['command'], *hook['args']], input=json.dumps(event), text=True, check=True)
(workspace / ('managed-resumed.py' if resuming else 'managed-app.py')).write_text("print('ok')\\n")
stop = {**common,'hook_event_name':'Stop','prompt_id':prompt_id,'last_assistant_message':('managed resumed' if resuming else 'managed done'),'stop_hook_active':False}
hook = settings['hooks']['Stop'][0]['hooks'][0]
subprocess.run([hook['command'], *hook['args']], input=json.dumps(stop), text=True, check=True)
""",
                encoding="utf-8",
            )
            fake.chmod(0o755)

            code = launch_task(
                str(workspace),
                str(paper),
                backend="claude",
                claude_bin=str(fake),
                claude_model="kimi-k3",
                token_file=str(token_file),
                claude_base_url="https://provider.example",
                progress=None,
            )
            self.assertEqual(code, 0)
            manifest_path = workspace / ".recording" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            provider = manifest["claude_provider"]
            self.assertEqual(provider["profile"], "kimi")
            self.assertEqual(provider["model"], "kimi-k3")
            self.assertEqual(provider["token_file"], str(token_file.resolve()))
            self.assertEqual(provider["last_selected_token_label"], "WORKING")
            self.assertNotIn("good-secret", manifest_path.read_text())
            settings_path = workspace / ".recording" / "claude-config" / "settings.json"
            self.assertTrue(settings_path.is_file())
            self.assertNotIn("good-secret", settings_path.read_text())

            first_tested_at = provider["last_token_tested_at"]
            code = resume_task(str(workspace), claude_bin=str(fake), progress=None)
            self.assertEqual(code, 0)
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["turn_count"], 2)
            self.assertGreaterEqual(
                manifest["claude_provider"]["last_token_tested_at"], first_tested_at
            )
            transcript = [
                json.loads(line)
                for line in (workspace / ".recording" / "transcript.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertEqual(transcript[1]["user_input"], "managed follow up")

            output = export_dataset(str(workspace), str(root / "exported"), progress=None)
            exported_manifest = json.loads(
                (output / "recording-manifest.json").read_text()
            )
            exported_provider = exported_manifest["claude_provider"]
            self.assertNotIn("token_file", exported_provider)
            self.assertNotIn("last_selected_token_label", exported_provider)
            self.assertEqual(exported_provider["model"], "kimi-k3")
            self.assertNotIn("good-secret", (output / "recording-manifest.json").read_text())

            token_file.unlink()
            with self.assertRaisesRegex(LauncherError, "token file not found"):
                resume_task(str(workspace), claude_bin=str(fake), progress=None)
            unchanged = json.loads(manifest_path.read_text())
            self.assertEqual(unchanged["state"], "completed")
            self.assertEqual(unchanged["resume_count"], 1)

    def test_managed_claude_prepare_only_validates_without_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper = root / "paper.pdf"
            paper.write_bytes(b"%PDF-1.4\n")
            token_file = root / "tokens.env"
            token_file.write_text("PRIMARY=not-probed\n", encoding="utf-8")
            workspace = root / "task"
            with patch(
                "paper_task_launcher.launcher.select_token",
                side_effect=AssertionError("prepare-only must not probe"),
            ):
                code = launch_task(
                    str(workspace),
                    str(paper),
                    backend="claude",
                    claude_bin="missing-is-allowed-for-prepare-only",
                    claude_model="claude-test",
                    token_file=str(token_file),
                    claude_base_url="https://provider.example",
                    prepare_only=True,
                    progress=None,
                )
            self.assertEqual(code, 0)
            manifest = json.loads(
                (workspace / ".recording" / "manifest.json").read_text()
            )
            self.assertEqual(manifest["claude_provider"]["profile"], "claude")
            self.assertNotIn(
                "last_selected_token_label", manifest["claude_provider"]
            )


if __name__ == "__main__":
    unittest.main()
