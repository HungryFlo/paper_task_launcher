from __future__ import annotations

import hashlib
import mmap
import os
import shutil
import tempfile
from pathlib import Path

from .errors import LauncherError


RUNTIME_ROOT_ENV = "PAPER_TASK_RUNTIME_ROOT"


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _staging_path(persistent_home: Path) -> Path:
    return persistent_home.parent / f".{persistent_home.name}.sync-staging"


def _backup_path(persistent_home: Path) -> Path:
    return persistent_home.parent / f".{persistent_home.name}.sync-backup"


def recover_harness_home(persistent_home: Path) -> None:
    """Recover a task home if a previous directory swap was interrupted."""
    persistent_home = Path(os.path.abspath(os.fspath(persistent_home)))
    staging = _staging_path(persistent_home)
    backup = _backup_path(persistent_home)
    if (
        not persistent_home.exists()
        and not persistent_home.is_symlink()
        and (backup.exists() or backup.is_symlink())
    ):
        backup.rename(persistent_home)
    elif backup.exists() or backup.is_symlink():
        _remove_path(backup)
    if staging.exists() or staging.is_symlink():
        _remove_path(staging)


class ManagedHarnessHome:
    """Run a harness from local storage and mirror its complete home to a task."""

    def __init__(
        self,
        persistent_home: Path,
        recording_id: str,
        *,
        require_mmap: bool = False,
    ) -> None:
        self.persistent_home = Path(os.path.abspath(os.fspath(persistent_home)))
        self.recording_id = recording_id
        self.require_mmap = require_mmap
        configured_root = os.environ.get(RUNTIME_ROOT_ENV)
        runtime_base = (
            Path(configured_root).expanduser()
            if configured_root
            else Path(tempfile.gettempdir())
        ).resolve()
        user_root = runtime_base / f"paper-task-harness-{os.getuid()}"
        identity = (
            f"{recording_id}\0{self.persistent_home}\0{self.persistent_home.name}"
        )
        task_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        self.runtime_task_dir = user_root / task_key
        self.runtime_home = self.runtime_task_dir / self.persistent_home.name
        self._entered = False

    def __enter__(self) -> ManagedHarnessHome:
        recover_harness_home(self.persistent_home)
        if self.persistent_home.is_symlink():
            raise LauncherError(
                f"Harness state directory must not be a symbolic link: {self.persistent_home}"
            )
        if self.persistent_home.exists() and not self.persistent_home.is_dir():
            raise LauncherError(
                f"Harness state path must be a directory: {self.persistent_home}"
            )
        self.persistent_home.mkdir(parents=True, exist_ok=True, mode=0o700)

        root = self.runtime_task_dir.parent
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            root.chmod(0o700)
        except OSError as exc:
            raise LauncherError(
                f"Unable to secure harness runtime root {root}: {exc}"
            ) from exc
        if self.runtime_task_dir.exists() or self.runtime_task_dir.is_symlink():
            _remove_path(self.runtime_task_dir)
        self.runtime_home.mkdir(parents=True, mode=0o700)
        try:
            shutil.copytree(
                self.persistent_home,
                self.runtime_home,
                dirs_exist_ok=True,
                symlinks=True,
            )
            self.runtime_home.chmod(0o700)
            if self.require_mmap:
                self._check_mmap()
        except Exception:
            _remove_path(self.runtime_task_dir)
            raise
        self._entered = True
        return self

    def _check_mmap(self) -> None:
        probe = self.runtime_home / ".paper-task-mmap-probe"
        try:
            with probe.open("w+b") as handle:
                handle.truncate(4096)
                mapping = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_WRITE)
                mapping.close()
        except OSError as exc:
            raise LauncherError(
                f"Harness runtime directory does not support writable mmap: "
                f"{self.runtime_home}: {exc}. Set {RUNTIME_ROOT_ENV} to local storage."
            ) from exc
        finally:
            probe.unlink(missing_ok=True)

    def _persist(self) -> None:
        staging = _staging_path(self.persistent_home)
        backup = _backup_path(self.persistent_home)
        recover_harness_home(self.persistent_home)
        try:
            shutil.copytree(
                self.runtime_home,
                staging,
                symlinks=True,
            )
            staging.chmod(0o700)
        except Exception as exc:
            _remove_path(staging)
            raise LauncherError(
                f"Unable to stage harness state for {self.persistent_home}: {exc}"
            ) from exc

        if backup.exists() or backup.is_symlink():
            _remove_path(backup)
        try:
            self.persistent_home.rename(backup)
            staging.rename(self.persistent_home)
        except OSError as exc:
            if not self.persistent_home.exists() and backup.exists():
                backup.rename(self.persistent_home)
            raise LauncherError(
                f"Unable to persist harness state to {self.persistent_home}: {exc}"
            ) from exc
        else:
            _remove_path(backup)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if not self._entered:
            return False
        sync_error: Exception | None = None
        try:
            self._persist()
        except Exception as caught:
            sync_error = caught
        if sync_error is None:
            _remove_path(self.runtime_task_dir)
        self._entered = False
        if sync_error is not None:
            if exc is not None:
                exc.add_note(f"Harness state sync also failed: {sync_error}")
                return False
            raise sync_error
        return False

    @staticmethod
    def _absolute(path: Path) -> Path:
        return Path(os.path.abspath(os.fspath(path)))

    def runtime_path_for(self, persistent_path: Path) -> Path | None:
        try:
            relative = self._absolute(persistent_path).relative_to(
                self._absolute(self.persistent_home)
            )
        except ValueError:
            return None
        return self.runtime_home / relative

    def persistent_path_for(self, runtime_path: Path) -> Path | None:
        try:
            relative = self._absolute(runtime_path).relative_to(
                self._absolute(self.runtime_home)
            )
        except ValueError:
            return None
        return self.persistent_home / relative


__all__ = [
    "ManagedHarnessHome",
    "RUNTIME_ROOT_ENV",
    "recover_harness_home",
]
