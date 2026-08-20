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

    def __init__(self, workspace: Path, recording_id: str):
        self.workspace = workspace
        self.recording_id = recording_id
        self.index_path = workspace / ".recording" / "snapshot.index"
        self.parent: str | None = None
        self.count = 0

    def _snapshot_paths(self) -> list[str]:
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
