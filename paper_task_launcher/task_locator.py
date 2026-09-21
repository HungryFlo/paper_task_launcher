from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .errors import LauncherError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "out"
WAVE_DIRECTORIES = tuple(f"wave{index}" for index in range(1, 5))


@dataclass(frozen=True)
class LocatedPaperTask:
    paper_id: str
    annotator: str
    paper: Path
    web: Path
    default_workspace: Path


def normalize_paper_id(value: str | int) -> str:
    raw = str(value).strip()
    if not re.fullmatch(r"[0-9]+", raw):
        raise LauncherError(f"Invalid paper ID {value!r}: expected a positive integer")
    normalized = raw.lstrip("0")
    if not normalized:
        raise LauncherError("Invalid paper ID 0: expected a positive integer")
    return normalized


def workspace_for_paper_id(
    paper_id: str | int,
    *,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> Path:
    return output_root.resolve() / normalize_paper_id(paper_id)


def ensure_default_output_root(output_root: Path = DEFAULT_OUTPUT_ROOT) -> Path:
    requested = output_root.expanduser()
    if requested.is_symlink() or (requested.exists() and not requested.is_dir()):
        raise LauncherError(
            f"Default output root must be a normal directory: {requested}"
        )
    root = requested.resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LauncherError(f"Unable to create default output root {root}: {exc}") from exc
    return root


def is_default_output_workspace(
    value: Path,
    *,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> bool:
    try:
        value.expanduser().resolve().relative_to(output_root.resolve())
    except ValueError:
        return False
    return True


def _matching_papers(annotator_dir: Path, paper_id: str) -> list[Path]:
    matches: list[Path] = []
    for wave_name in WAVE_DIRECTORIES:
        wave_dir = annotator_dir / wave_name
        if not wave_dir.is_dir():
            continue
        for candidate in wave_dir.iterdir():
            if not candidate.is_file() or candidate.suffix.lower() != ".pdf":
                continue
            if candidate.stem == paper_id or candidate.stem.startswith(f"{paper_id}_"):
                matches.append(candidate.resolve())
    return sorted(matches)


def locate_paper_task(
    paper_id: str | int,
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> LocatedPaperTask:
    normalized_id = normalize_paper_id(paper_id)
    root = data_root.expanduser().resolve()
    if not root.is_dir():
        raise LauncherError(
            f"Data directory does not exist: {root}. Move the annotator directory "
            "under <paper_task_launcher>/data first."
        )

    complete: list[tuple[Path, Path, Path]] = []
    paper_only: list[tuple[str, Path]] = []
    web_only: list[tuple[str, Path]] = []
    duplicate_papers: list[tuple[str, list[Path]]] = []

    for annotator_dir in sorted(root.iterdir(), key=lambda path: path.name):
        if not annotator_dir.is_dir() or annotator_dir.name.startswith("."):
            continue
        papers = _matching_papers(annotator_dir, normalized_id)
        web = annotator_dir / "webs" / f"{normalized_id}_web"
        has_web = web.is_dir()
        if len(papers) > 1:
            duplicate_papers.append((annotator_dir.name, papers))
            continue
        if len(papers) == 1 and has_web:
            complete.append((annotator_dir, papers[0], web.resolve()))
        elif len(papers) == 1:
            paper_only.append((annotator_dir.name, papers[0]))
        elif has_web:
            web_only.append((annotator_dir.name, web.resolve()))

    if duplicate_papers:
        details = "; ".join(
            f"{annotator}: {', '.join(str(path) for path in papers)}"
            for annotator, papers in duplicate_papers
        )
        raise LauncherError(
            f"paperID {normalized_id} matches multiple PDFs in one annotator directory: "
            f"{details}"
        )
    if len(complete) > 1:
        annotators = ", ".join(candidate[0].name for candidate in complete)
        raise LauncherError(
            f"paperID {normalized_id} is ambiguous across annotators: {annotators}. "
            "Keep only the current annotator's directory under data, or use explicit "
            "--paper, --web, and --workspace paths."
        )
    if not complete:
        details: list[str] = []
        if paper_only:
            details.append(
                "PDF found but matching webs/<ID>_web missing for "
                + ", ".join(name for name, _ in paper_only)
            )
        if web_only:
            details.append(
                "Web found but matching wave1-wave4 PDF missing for "
                + ", ".join(name for name, _ in web_only)
            )
        suffix = f" ({'; '.join(details)})" if details else ""
        raise LauncherError(
            f"Unable to locate paperID {normalized_id} under {root}. Expected one "
            f"annotator directory containing wave1-wave4/<ID>_*.pdf and "
            f"webs/<ID>_web{suffix}"
        )

    annotator_dir, paper, web = complete[0]
    return LocatedPaperTask(
        paper_id=normalized_id,
        annotator=annotator_dir.name,
        paper=paper,
        web=web,
        default_workspace=workspace_for_paper_id(
            normalized_id,
            output_root=output_root,
        ),
    )


__all__ = [
    "DEFAULT_DATA_ROOT",
    "DEFAULT_OUTPUT_ROOT",
    "LocatedPaperTask",
    "PROJECT_ROOT",
    "ensure_default_output_root",
    "is_default_output_workspace",
    "locate_paper_task",
    "normalize_paper_id",
    "workspace_for_paper_id",
]
