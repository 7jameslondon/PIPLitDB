"""Allowlisted source discovery for one private record."""

from __future__ import annotations

import mimetypes
from pathlib import Path
import zipfile

from .models import SourceFile
from .paths import ensure_within, reject_reparse_chain, sha256_file, validate_record_id


def detect_format(path: Path) -> str:
    with path.open("rb") as stream:
        header = stream.read(16)
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as package:
                info = package.getinfo("[Content_Types].xml")
                if info.file_size > 2 * 1024 * 1024:
                    return "application/zip"
                content_types = package.read(info)
        except (KeyError, OSError, zipfile.BadZipFile, RuntimeError):
            return "application/zip"
        if b"presentationml.presentation.main+xml" in content_types:
            return (
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            )
        if b"spreadsheetml.sheet.main+xml" in content_types:
            return (
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            )
        if b"wordprocessingml.document.main+xml" in content_types:
            return (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            )
        return "application/zip"
    if header.startswith((b"\xff\xd8\xff",)):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header[:4] in {b"II*\x00", b"MM\x00*"}:
        return "image/tiff"
    if path.suffix.lower() in {".html", ".htm"}:
        return "text/html"
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _pdf_page_count(path: Path) -> int | None:
    if detect_format(path) != "application/pdf":
        return None
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError:
        return None
    return len(PdfReader(path).pages)


def _source(role: str, path: Path, repository_root: Path) -> SourceFile:
    return SourceFile(
        role=role,
        path=path,
        relative_path=path.relative_to(repository_root).as_posix(),
        size=path.stat().st_size,
        sha256=sha256_file(path),
        detected_format=detect_format(path),
        page_count=_pdf_page_count(path),
    )


def discover_sources(repository_root: Path, record_id: str) -> list[SourceFile]:
    record_id = validate_record_id(record_id)
    repository_root = repository_root.resolve(strict=True)
    private_root = ensure_within(repository_root / "papers (private)", repository_root)
    record_root = ensure_within(private_root / record_id, private_root)
    reject_reparse_chain(record_root, private_root)

    candidates: list[tuple[str, Path]] = []
    metadata_root = repository_root / "database" / "records"
    metadata = metadata_root / f"{record_id}.yaml"
    if not metadata.is_file():
        raise FileNotFoundError(f"public record metadata is missing: {metadata}")
    candidates.append(("public_metadata", metadata))

    main_pdf = record_root / "pdf" / "main.pdf"
    main_html = record_root / "html" / "main.html"
    if main_pdf.is_file():
        candidates.append(("main_pdf", main_pdf))
    if main_html.is_file():
        candidates.append(("main_html", main_html))

    supplementary_root = record_root / "supplementary"
    if supplementary_root.exists():
        reject_reparse_chain(supplementary_root, record_root)
        for path in sorted(
            supplementary_root.rglob("*"),
            key=lambda item: item.relative_to(supplementary_root).as_posix().casefold(),
        ):
            if path.is_dir():
                if is_unsafe_entry(path):
                    raise ValueError(f"unsafe supplementary directory: {path}")
                continue
            if is_unsafe_entry(path):
                raise ValueError(f"unsafe supplementary file: {path}")
            ensure_within(path, supplementary_root)
            candidates.append(("supplement", path))

    for role, path in candidates:
        approved_root = metadata_root if role == "public_metadata" else record_root
        ensure_within(path, approved_root)
        reject_reparse_chain(path, approved_root)
        if is_unsafe_entry(path):
            raise ValueError(f"unsafe {role} source: {path}")

    sources = [_source(role, path, repository_root) for role, path in candidates]
    publication_sources = [source for source in sources if source.role != "public_metadata"]
    if not publication_sources:
        raise FileNotFoundError(f"record {record_id} contains no publication sources")
    return sources


def is_unsafe_entry(path: Path) -> bool:
    return path.is_symlink() or path.is_socket() or path.is_fifo() or path.is_block_device()


def source_fingerprint(sources: list[SourceFile], override_hash: str | None = None) -> str:
    import hashlib
    import json

    payload = [
        {
            "role": source.role,
            "path": source.relative_path,
            "sha256": source.sha256,
            "bytes": source.size,
        }
        for source in sorted(sources, key=lambda item: (item.role, item.relative_path))
    ]
    if override_hash is not None:
        payload.append({"role": "private_override", "sha256": override_hash})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
