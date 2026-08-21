from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import socket
import ssl
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Callable, TypeVar

from .errors import LauncherError
from .util import directory_digest, utc_now

ARXIV_API_ENDPOINTS = (
    "http://export.arxiv.org/api/query",
    "https://export.arxiv.org/api/query",
)
ARXIV_SOURCE_ENDPOINTS = (
    "https://arxiv.org/e-print/{arxiv_id}",
    "https://export.arxiv.org/e-print/{arxiv_id}",
    "http://export.arxiv.org/e-print/{arxiv_id}",
)
USER_AGENT = "paper-task-launcher/0.2 (research tooling)"
MAX_DOWNLOAD = 512 * 1024 * 1024
MAX_ARCHIVE_FILES = 20_000
MAX_API_RESPONSE = 8 * 1024 * 1024
NETWORK_TIMEOUT = float(os.environ.get("PAPER_TASK_NETWORK_TIMEOUT", "20"))
NETWORK_RETRIES = max(1, int(os.environ.get("PAPER_TASK_NETWORK_RETRIES", "2")))

Progress = Callable[[str], None]
T = TypeVar("T")

_NEW_ID = r"\d{4}\.\d{4,5}(?:v\d+)?"
_OLD_ID = r"[a-zA-Z][a-zA-Z0-9.\-]+/\d{7}(?:v\d+)?"
_ARXIV_ID = re.compile(rf"^(?:arXiv:)?({_NEW_ID}|{_OLD_ID})$", re.I)
_ARXIV_URL = re.compile(
    rf"https?://(?:export\.)?arxiv\.org/(?:abs|pdf|html|e-print)/({_NEW_ID}|{_OLD_ID})(?:\.pdf)?(?:[?#].*)?$",
    re.I,
)


@dataclass
class PaperMetadata:
    input: str
    kind: str
    imported_at: str
    source_sha256: str
    file_count: int
    arxiv_id: str | None = None
    title: str | None = None
    authors: list[str] | None = None
    api_entry_url: str | None = None
    downloaded_archive_sha256: str | None = None
    downloaded_archive_url: str | None = None
    metadata_api_url: str | None = None
    input_pdf_sha256: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def extract_arxiv_id(value: str) -> str | None:
    plain = _ARXIV_ID.fullmatch(value.strip())
    if plain:
        return plain.group(1)
    parsed = urllib.parse.unquote(value.strip())
    matched = _ARXIV_URL.fullmatch(parsed)
    return matched.group(1) if matched else None


def _network_operation(
    urls: list[str],
    operation: Callable[[str], T],
    *,
    progress: Progress | None = None,
) -> T:
    failures: list[str] = []
    for endpoint_index, url in enumerate(urls):
        if endpoint_index:
            if progress:
                progress("主端点不可用，正在切换 arXiv 备用端点…")
        for attempt in range(1, NETWORK_RETRIES + 1):
            try:
                return operation(url)
            except urllib.error.HTTPError as exc:
                failures.append(f"{url}: HTTP {exc.code}")
                # A malformed request will fail identically on every endpoint.
                if exc.code == 400:
                    raise LauncherError(f"arXiv rejected the request: HTTP {exc.code}") from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                socket.timeout,
                ssl.SSLError,
                ConnectionError,
                OSError,
            ) as exc:
                failures.append(f"{url}: {exc}")
            if attempt < NETWORK_RETRIES:
                delay = 3 * attempt
                if progress:
                    progress(
                        f"arXiv 请求失败，{delay} 秒后重试 "
                        f"({attempt + 1}/{NETWORK_RETRIES})…"
                    )
                time.sleep(delay)
    detail = failures[-1] if failures else "unknown network error"
    raise LauncherError(
        "多次重试并切换备用端点后仍无法连接 arXiv。"
        f"最后一次失败：{detail}。请检查代理或防火墙，也可以增大 "
        "PAPER_TASK_NETWORK_TIMEOUT。"
    )


def _open(url: str) -> urllib.response.addinfourl:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT)


def _fetch_bytes(urls: list[str], *, progress: Progress | None = None) -> tuple[bytes, str]:
    def fetch(url: str) -> tuple[bytes, str]:
        with _open(url) as response:
            data = response.read(MAX_API_RESPONSE + 1)
        if len(data) > MAX_API_RESPONSE:
            raise LauncherError("arXiv API response is unexpectedly large")
        return data, url

    return _network_operation(urls, fetch, progress=progress)


def _download(
    urls: str | list[str],
    destination: Path,
    *,
    progress: Progress | None = None,
) -> tuple[str, int, str]:
    candidates = [urls] if isinstance(urls, str) else urls

    def download(url: str) -> tuple[str, int, str]:
        digest = hashlib.sha256()
        total = 0
        try:
            with _open(url) as response, destination.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD:
                        raise LauncherError(f"Download exceeds {MAX_DOWNLOAD} bytes: {url}")
                    digest.update(chunk)
                    output.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        if total == 0:
            destination.unlink(missing_ok=True)
            raise LauncherError(f"Downloaded an empty response: {url}")
        return digest.hexdigest(), total, url

    return _network_operation(candidates, download, progress=progress)


def _copy_local_source(source: Path, destination: Path) -> None:
    destination_resolved = destination.resolve(strict=False)
    try:
        destination_resolved.relative_to(source)
        raise LauncherError("Local paper source must be outside the task workspace")
    except ValueError:
        pass
    try:
        source.relative_to(destination_resolved)
        raise LauncherError("Local paper source must be outside the task workspace")
    except ValueError:
        pass
    for path in source.rglob("*"):
        if path.is_symlink():
            raise LauncherError(f"Paper source contains a symbolic link: {path}")

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {".git"} if ".git" in names else set()

    shutil.copytree(source, destination, ignore=ignore)


def _safe_archive_path(name: str) -> Path:
    normalized = PurePosixPath(name)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise LauncherError(f"Unsafe path in arXiv source archive: {name}")
    clean = Path(*[part for part in normalized.parts if part not in {"", "."}])
    if not clean.parts:
        raise LauncherError("The arXiv source archive contains an empty path")
    return clean


def _extract_source_archive(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    if tarfile.is_tarfile(archive):
        total_size = 0
        file_count = 0
        with tarfile.open(archive, "r:*") as tar:
            for member in tar.getmembers():
                relative = _safe_archive_path(member.name)
                target = destination / relative
                if member.issym() or member.islnk() or member.isdev():
                    raise LauncherError(
                        f"Unsupported link/device in arXiv source archive: {member.name}"
                    )
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                file_count += 1
                total_size += member.size
                if file_count > MAX_ARCHIVE_FILES or total_size > MAX_DOWNLOAD:
                    raise LauncherError("The arXiv source archive is too large")
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise LauncherError(f"Cannot extract archive member: {member.name}")
                with extracted, target.open("wb") as output:
                    shutil.copyfileobj(extracted, output)
        return

    with archive.open("rb") as handle:
        magic = handle.read(2)
    if magic == b"\x1f\x8b":
        target = destination / "source.tex"
        try:
            with gzip.open(archive, "rb") as source, target.open("wb") as output:
                total = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD:
                        raise LauncherError("The decompressed arXiv source is too large")
                    output.write(chunk)
        except OSError as exc:
            raise LauncherError(f"Invalid gzip source archive: {exc}") from exc
        return

    if archive.stat().st_size > MAX_DOWNLOAD:
        raise LauncherError("The arXiv source file is too large")
    with archive.open("rb") as handle:
        if handle.read(5) == b"%PDF-":
            raise LauncherError("This arXiv submission provides a PDF but no LaTeX source")
    shutil.copy2(archive, destination / "source.tex")


def _require_latex_source(root: Path) -> None:
    latex_extensions = {".tex", ".ltx", ".latex"}
    if not any(
        path.is_file() and path.suffix.lower() in latex_extensions
        for path in root.rglob("*")
    ):
        raise LauncherError("The imported paper source contains no LaTeX source file")


def _parse_atom(data: bytes) -> list[dict]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise LauncherError(f"Invalid response from arXiv API: {exc}") from exc
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = []
    for entry in root.findall("atom:entry", ns):
        title = " ".join((entry.findtext("atom:title", default="", namespaces=ns)).split())
        url = entry.findtext("atom:id", default="", namespaces=ns)
        authors = [
            " ".join((author.findtext("atom:name", default="", namespaces=ns)).split())
            for author in entry.findall("atom:author", ns)
        ]
        arxiv_id = url.rstrip("/").split("/")[-1]
        entries.append({"id": arxiv_id, "title": title, "authors": authors, "url": url})
    return entries


def query_arxiv_by_id(arxiv_id: str, *, progress: Progress | None = None) -> dict:
    params = urllib.parse.urlencode({"id_list": arxiv_id, "max_results": 1})
    urls = [f"{endpoint}?{params}" for endpoint in ARXIV_API_ENDPOINTS]
    data, used_url = _fetch_bytes(urls, progress=progress)
    entries = _parse_atom(data)
    if not entries:
        raise LauncherError(f"No arXiv paper found for ID {arxiv_id}")
    entries[0]["metadata_api_url"] = used_url
    return entries[0]


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _extract_pdf_title(pdf: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "1", str(pdf), "-"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise LauncherError(
            "A non-arXiv PDF URL requires the 'pdftotext' command to identify the paper"
        ) from exc
    if result.returncode != 0:
        raise LauncherError(f"Could not read PDF title: {result.stderr.strip()}")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    candidates = [line for line in lines[:12] if 5 <= len(line) <= 300]
    if not candidates:
        raise LauncherError("Could not identify a title on the first page of the PDF")
    # Most papers place the title in the first few non-empty lines. Join wrapped title lines
    # until an obvious author/email/affiliation marker is encountered.
    title_parts: list[str] = []
    for line in candidates:
        lower = line.lower()
        if title_parts and ("@" in line or re.search(r"\b(university|institute|laboratory)\b", lower)):
            break
        if title_parts and re.fullmatch(r"[\w .,'\-]+(?:,| and )[\w .,'\-]+", line):
            break
        title_parts.append(line)
        if len(" ".join(title_parts)) > 220:
            break
    return " ".join(title_parts)


def search_arxiv_for_pdf(
    pdf_url: str, *, progress: Progress | None = None
) -> tuple[dict, str]:
    with tempfile.TemporaryDirectory(prefix="paper-task-pdf-") as temporary:
        pdf = Path(temporary) / "input.pdf"
        pdf_hash, _pdf_size, _pdf_used_url = _download(
            pdf_url, pdf, progress=progress
        )
        title = _extract_pdf_title(pdf)
    params = urllib.parse.urlencode(
        {"search_query": f'ti:"{title}"', "start": 0, "max_results": 5}
    )
    urls = [f"{endpoint}?{params}" for endpoint in ARXIV_API_ENDPOINTS]
    data, used_url = _fetch_bytes(urls, progress=progress)
    entries = _parse_atom(data)
    if not entries:
        raise LauncherError(f"No arXiv result matched the PDF title: {title}")
    target = _normalize_title(title)
    scored = sorted(
        ((SequenceMatcher(None, target, _normalize_title(e["title"])).ratio(), e) for e in entries),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < 0.82 or best_score - runner_up < 0.05:
        candidates = "; ".join(f"{score:.2f} {entry['id']} {entry['title']}" for score, entry in scored)
        raise LauncherError(
            "The PDF could not be mapped unambiguously to arXiv. "
            f"Use an arXiv URL or ID. Candidates: {candidates}"
        )
    best["metadata_api_url"] = used_url
    return best, pdf_hash


def import_paper(
    source_value: str,
    destination: Path,
    *,
    progress: Progress | None = None,
) -> PaperMetadata:
    local = Path(source_value).expanduser()
    if local.exists():
        if not local.is_dir():
            raise LauncherError("A local paper source must be a directory")
        source = local.resolve()
        if progress:
            progress(f"正在复制本地 LaTeX 源码：{source}")
        _copy_local_source(source, destination)
        _require_latex_source(destination)
        digest, count = directory_digest(destination)
        if count == 0:
            raise LauncherError("The local paper source directory contains no files")
        metadata = PaperMetadata(
            input=str(source),
            kind="local_latex_directory",
            imported_at=utc_now(),
            source_sha256=digest,
            file_count=count,
        )
        if progress:
            progress(f"论文源码导入完成，共 {count} 个文件")
        return metadata

    parsed = urllib.parse.urlparse(source_value)
    arxiv_id = extract_arxiv_id(source_value)
    pdf_hash = None
    if arxiv_id:
        if progress:
            progress(f"已识别 arXiv ID：{arxiv_id}，将直接下载论文源码…")
        # An explicit arXiv ID already identifies the source archive unambiguously.
        # Do not make successful import depend on the less reliable metadata API.
        entry = {
            "id": arxiv_id,
            "title": None,
            "authors": None,
            "url": f"https://arxiv.org/abs/{arxiv_id}",
        }
    elif parsed.scheme in {"http", "https"}:
        if progress:
            progress("正在读取 PDF 并通过 arXiv API 匹配论文…")
        entry, pdf_hash = search_arxiv_for_pdf(source_value, progress=progress)
        arxiv_id = entry["id"]
    else:
        raise LauncherError(
            "Paper input must be a local LaTeX directory, an arXiv ID/URL, or a PDF URL"
        )

    assert arxiv_id is not None
    if progress:
        if entry.get("title"):
            progress(f"已找到论文：{entry['title']}")
        progress("正在下载 arXiv LaTeX 源码包…")
    with tempfile.TemporaryDirectory(prefix="paper-task-arxiv-") as temporary:
        archive = Path(temporary) / "source"
        quoted_id = urllib.parse.quote(arxiv_id, safe="/")
        source_urls = [
            endpoint.format(arxiv_id=quoted_id)
            for endpoint in ARXIV_SOURCE_ENDPOINTS
        ]
        archive_hash, archive_size, source_url = _download(
            source_urls, archive, progress=progress
        )
        if progress:
            progress(f"源码包下载完成（{archive_size / 1024 / 1024:.1f} MiB），正在解压…")
        _extract_source_archive(archive, destination)
    _require_latex_source(destination)
    digest, count = directory_digest(destination)
    if count == 0:
        raise LauncherError(f"arXiv source archive for {arxiv_id} contains no files")
    metadata = PaperMetadata(
        input=source_value,
        kind="arxiv_source",
        imported_at=utc_now(),
        source_sha256=digest,
        file_count=count,
        arxiv_id=arxiv_id,
        title=entry.get("title"),
        authors=entry.get("authors"),
        api_entry_url=entry.get("url"),
        downloaded_archive_sha256=archive_hash,
        downloaded_archive_url=source_url,
        metadata_api_url=entry.get("metadata_api_url"),
        input_pdf_sha256=pdf_hash,
    )
    if progress:
        progress(f"论文源码导入完成，共 {count} 个文件")
    return metadata
