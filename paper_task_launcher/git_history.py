from __future__ import annotations

import os
from pathlib import Path

from .errors import LauncherError
from .util import run_checked, utc_now


GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "paper-task-recorder",
    "GIT_AUTHOR_EMAIL": "paper-task-recorder@localhost",
    "GIT_COMMITTER_NAME": "paper-task-recorder",
    "GIT_COMMITTER_EMAIL": "paper-task-recorder@localhost",
}

WEB_EXCLUDED_DIRECTORIES = {
    ".git",
    ".recording",
    ".claude",
    ".codex",
    ".kimi",
    ".kimi-code",
    "paper-source",
    "node_modules",
    "__pycache__",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "out_data",
}
WEB_EXCLUDED_FILES = {".npmrc", ".pypirc", ".DS_Store"}


def is_safe_web_path(relative: Path, *, directory: bool = False) -> bool:
    parts = relative.parts
    if any(part in WEB_EXCLUDED_DIRECTORIES for part in parts):
        return False
    if directory:
        return True
    name = relative.name
    if name in WEB_EXCLUDED_FILES:
        return False
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return False
    if name.lower().endswith((".pem", ".key", ".p12", ".pfx")):
        return False
    return True


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.update(GIT_IDENTITY)
    if extra:
        env.update(extra)
    return env


def is_inside_existing_worktree(path: Path) -> bool:
    parent = path.parent
    try:
        output = run_checked(["git", "-C", str(parent), "rev-parse", "--show-toplevel"])
    except LauncherError:
        return False
    root = Path(output).resolve()
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def initialize_repository(workspace: Path) -> None:
    run_checked(["git", "init", "-b", "main", str(workspace)])
    run_checked(["git", "add", ".gitignore"], cwd=workspace)
    run_checked(
        ["git", "commit", "-m", "Initialize isolated paper task"],
        cwd=workspace,
        env=_git_env(),
    )


class SnapshotStore:
    """Create Git commits on private refs without modifying HEAD or the normal index."""

    def __init__(
        self,
        workspace: Path,
        recording_id: str,
        *,
        snapshot_policy: str = "git",
    ):
        if snapshot_policy not in {"git", "web"}:
            raise LauncherError(f"Unsupported snapshot policy: {snapshot_policy}")
        self.workspace = workspace
        self.recording_id = recording_id
        self.snapshot_policy = snapshot_policy
        self.index_path = workspace / ".recording" / "snapshot.index"
        self.parent: str | None = None
        self.count = 0

    def _web_snapshot_paths(self) -> list[str]:
        paths: set[str] = set()
        for current, directory_names, file_names in os.walk(
            self.workspace, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            kept_directories: list[str] = []
            for name in directory_names:
                candidate = current_path / name
                relative = candidate.relative_to(self.workspace)
                if not is_safe_web_path(relative, directory=True):
                    continue
                if candidate.is_symlink():
                    paths.add(relative.as_posix())
                else:
                    kept_directories.append(name)
            directory_names[:] = kept_directories
            for name in file_names:
                candidate = current_path / name
                relative = candidate.relative_to(self.workspace)
                if is_safe_web_path(relative):
                    paths.add(relative.as_posix())
        return sorted(paths)

    def _snapshot_paths(self) -> list[str]:
        if self.snapshot_policy == "web":
            return self._web_snapshot_paths()
        tracked = run_checked(
            ["git", "ls-files", "--cached", "-z"], cwd=self.workspace
        ).split("\0")
        untracked = run_checked(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=self.workspace,
        ).split("\0")
        paths: set[str] = set()
        for relative in tracked + untracked:
            if not relative:
                continue
            if relative == "paper-source" or relative.startswith("paper-source/"):
                continue
            if relative == ".recording" or relative.startswith(".recording/"):
                continue
            if relative == "out_data" or relative.startswith("out_data/"):
                continue
            candidate = self.workspace / relative
            if candidate.exists() or candidate.is_symlink():
                paths.add(relative)
        return sorted(paths)

    def capture(
        self,
        label: str,
        *,
        turn_id: str | None = None,
        ref_name: str | None = None,
        advance: bool = True,
    ) -> dict:
        try:
            self.index_path.unlink()
        except FileNotFoundError:
            pass
        env = _git_env({"GIT_INDEX_FILE": str(self.index_path)})
        run_checked(["git", "read-tree", "--empty"], cwd=self.workspace, env=env)
        paths = self._snapshot_paths()
        if paths:
            run_checked(
                [
                    "git",
                    "add",
                    "-f",
                    "--pathspec-from-file=-",
                    "--pathspec-file-nul",
                ],
                cwd=self.workspace,
                env=env,
                input_text="\0".join(paths) + "\0",
            )
        tree = run_checked(["git", "write-tree"], cwd=self.workspace, env=env)
        message = f"paper-task snapshot {label}"
        args = ["git", "commit-tree", tree, "-m", message]
        if self.parent:
            args.extend(["-p", self.parent])
        commit = run_checked(args, cwd=self.workspace, env=env)
        if advance:
            self.count += 1
        suffix = ref_name or f"turn/{self.count:04d}"
        ref = f"refs/recorder/{self.recording_id}/{suffix}"
        run_checked(["git", "update-ref", ref, commit], cwd=self.workspace, env=env)
        run_checked(
            ["git", "update-ref", f"refs/recorder/{self.recording_id}/latest", commit],
            cwd=self.workspace,
            env=env,
        )
        self.parent = commit
        return {
            "index": self.count,
            "label": label,
            "turn_id": turn_id,
            "commit": commit,
            "tree": tree,
            "ref": ref,
            "captured_at": utc_now(),
        }
