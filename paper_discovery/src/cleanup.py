"""Remove only verified redundant legacy responses; preserve source evidence."""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import uuid

from .config import DiscoveryError
from .linked_client import save_json
from .store import now, writer_lock


def redirected(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def checked_path(staging, path):
    if not path.is_relative_to(staging) or path == staging:
        raise DiscoveryError("Cleanup target must be below the staging directory")
    current = path
    while current != staging:
        if redirected(current):
            raise DiscoveryError("Cleanup refuses symbolic links and junctions")
        current = current.parent
    resolved = path.resolve()
    if not resolved.is_relative_to(staging) or resolved == staging:
        raise DiscoveryError("Cleanup target escapes the staging directory")
    return resolved


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def duplicate_plan(work):
    staging = (work.resolve() / "staging")
    if redirected(staging):
        raise DiscoveryError("Cleanup refuses a redirected staging directory")
    candidates = []
    if staging.exists():
        for directory, dirs, files in os.walk(staging, followlinks=False):
            dirs[:] = [name for name in dirs if not redirected(Path(directory) / name)]
            folder = Path(directory)
            if folder.name != "responses":
                continue
            for name in sorted(files):
                if not name.endswith(".json"):
                    continue
                redundant, retained = folder / name, folder.parent / "raw" / name
                if not retained.is_file():
                    continue
                try:
                    redundant = checked_path(staging, redundant)
                    retained = checked_path(staging, retained)
                except DiscoveryError:
                    continue
                size = redundant.stat().st_size
                if not os.path.samefile(redundant, retained) and size == retained.stat().st_size:
                    sha = digest(redundant)
                    if sha == digest(retained):
                        candidates.append({"remove": str(redundant.relative_to(staging)),
                                           "retain": str(retained.relative_to(staging)), "bytes": size, "sha256": sha})
    return {"staging_directory": str(staging), "candidates": candidates,
            "reclaimable_bytes": sum(row["bytes"] for row in candidates)}


def cleanup(work, *, apply=False):
    plan = duplicate_plan(work)
    result = {"checked_at": now(), "dry_run": not apply, **plan, "deleted": [], "skipped_changed": [], "reclaimed_bytes": 0}
    if not apply:
        return result
    staging = Path(plan["staging_directory"])
    report = staging / "cleanup" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8] + ".json")
    checked_path(staging, report)
    result["report_path"] = str(report)
    save_json(report, result)
    for candidate in plan["candidates"]:
        redundant = checked_path(staging, staging / candidate["remove"])
        retained = checked_path(staging, staging / candidate["retain"])
        # Current linked runs use this same run-local lock. There is no recursive
        # removal and no deletion of raw responses, metadata, reviews or SQLite.
        with writer_lock(redundant.parent.parent):
            if (not redundant.is_file() or not retained.is_file() or
                    redundant.stat().st_size != candidate["bytes"] or retained.stat().st_size != candidate["bytes"] or
                    digest(redundant) != candidate["sha256"] or digest(retained) != candidate["sha256"]):
                result["skipped_changed"].append(candidate["remove"])
            else:
                redundant.unlink()
                result["deleted"].append(candidate["remove"])
                result["reclaimed_bytes"] += candidate["bytes"]
        save_json(report, result)
    return result
