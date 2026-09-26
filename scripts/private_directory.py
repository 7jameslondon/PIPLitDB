"""Race-safe allocation of private diagnostic output directories."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import secrets


def create_unique_private_directory(
    parent: Path,
    prefix: str,
    *,
    attempts: int = 128,
) -> Path:
    """Create one new child without overwriting or retrying permission errors.

    Only an actual name collision is retried.  In particular, ``PermissionError``
    propagates on the first failed ``mkdir`` instead of entering the standard
    library's Windows temporary-name retry loop.  Windows directories use the
    parent's private ACL; POSIX directories are explicitly owner-only.
    """

    if not prefix or Path(prefix).name != prefix or prefix in {".", ".."}:
        raise ValueError("private directory prefix must be a non-empty leaf name")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise ValueError("private directory attempts must be a positive integer")

    mode = 0o777 if os.name == "nt" else 0o700
    for _ in range(attempts):
        candidate = parent / f"{prefix}{secrets.token_hex(8)}"
        try:
            candidate.mkdir(mode=mode)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(
        errno.EEXIST,
        "could not allocate a unique private diagnostic directory",
        str(parent),
    )


__all__ = ["create_unique_private_directory"]
