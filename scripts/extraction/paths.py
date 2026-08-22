"""Path containment and deterministic file-writing helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable


RECORD_ID_PATTERN = re.compile(r"^\d{5}$")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


class UnsafePathError(ValueError):
    """Raised when a path escapes the approved private boundary."""


def validate_record_id(record_id: str) -> str:
    if not RECORD_ID_PATTERN.fullmatch(record_id):
        raise ValueError("record ID must contain exactly five digits")
    return record_id


def validate_run_id(run_id: str) -> str:
    """Return a portable staged-run name or raise :class:`ValueError`.

    The extraction tree is shared across Windows and POSIX systems.  In
    addition to excluding separators and traversal, reject names Windows
    silently normalizes or reserves so a run always identifies one directory.
    """

    if (
        not RUN_ID_PATTERN.fullmatch(run_id)
        or run_id in {".", ".."}
        or run_id.endswith((".", " "))
        or run_id.split(".", 1)[0].upper() in WINDOWS_DEVICE_NAMES
    ):
        raise ValueError(
            "run ID must be 1-64 portable filename characters, begin with a "
            "letter or digit, and not be a reserved Windows filename"
        )
    return run_id


def is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return path.is_symlink()
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def ensure_within(path: Path, root: Path, *, require_exists: bool = True) -> Path:
    resolved_root = root.resolve(strict=True)
    resolved_path = path.resolve(strict=require_exists)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise UnsafePathError(f"path escapes approved root: {path}") from exc
    return resolved_path


def reject_reparse_chain(path: Path, stop: Path) -> None:
    resolved_stop = stop.resolve(strict=True)
    current = path
    while True:
        if current.exists() and is_reparse_point(current):
            raise UnsafePathError(f"reparse points are not allowed: {current}")
        if current.resolve(strict=False) == resolved_stop:
            return
        if current.parent == current:
            raise UnsafePathError(f"path is not beneath approved root: {path}")
        current = current.parent


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
    ) + "\n"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, canonical_json(value))


def atomic_write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    lines = [
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for value in values
    ]
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))
