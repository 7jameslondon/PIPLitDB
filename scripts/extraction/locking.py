"""One fail-fast coordinator lock for shared extraction mutations."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .paths import ensure_within, reject_reparse_chain


OWNER_WORKSPACE_MARKER = ".extraction-owner.json"


@contextmanager
def repository_lock(repository_root: Path):
    """Serialize publication, queue updates and owner-workspace imports.

    A crashed holder leaves an inspectable lock. Never steal or expire it:
    recovery requires the coordinator to establish that the holder stopped.
    """
    root = repository_root.resolve(strict=True)
    private = root / "papers (private)"
    reject_reparse_chain(private, root)
    private.mkdir(exist_ok=True)
    ensure_within(private, root)
    lock = private / ".extraction-coordinator.lock"
    reject_reparse_chain(lock, private)
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise RuntimeError(f"extraction coordinator is busy; inspect {lock}") from exc
    owner = lock / "owner.json"
    try:
        owner.write_text(json.dumps({
            "pid": os.getpid(),
            "created_utc": datetime.now(timezone.utc).isoformat(),
        }) + "\n", encoding="utf-8")
        yield
    finally:
        owner.unlink(missing_ok=True)
        lock.rmdir()
