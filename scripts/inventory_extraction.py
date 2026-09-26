#!/usr/bin/env python3
"""Save complete private inventories and report bounded listings or changes.

Sources and inventoried files are read-only. New snapshots are written only to
the explicit --save-dir, outside the inventoried tree. No review is performed.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import stat
import sys

if __package__:
    from .inspect_extraction import _bounded_int, _fits, _serialized, _unique_object
    from .private_directory import create_unique_private_directory
    from .extraction.paths import is_reparse_point, reject_reparse_chain, sha256_file, validate_record_id
else:
    from inspect_extraction import _bounded_int, _fits, _serialized, _unique_object
    from private_directory import create_unique_private_directory
    from extraction.paths import is_reparse_point, reject_reparse_chain, sha256_file, validate_record_id


SNAPSHOT_VERSION = "extraction-inventory-1"


def _fingerprint(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _generator(mode: str) -> dict:
    directory = Path(__file__).resolve().parent
    names = [
        "inventory_extraction.py",
        "inspect_extraction.py",
        "private_directory.py",
        "extraction/paths.py",
    ]
    if mode == "sources":
        names += ["extraction/discovery.py", "extraction/models.py"]
    result = {"code_sha256": _fingerprint({name: sha256_file(directory / name) for name in names}),
              "python": platform.python_version()}
    if mode == "sources":
        try:
            result["pypdf"] = importlib.metadata.version("pypdf")
        except importlib.metadata.PackageNotFoundError:
            result["pypdf"] = "not-installed"
    return result


def _file_entry(path: Path, relative: str) -> dict:
    before = path.stat()
    digest = sha256_file(path)
    after = path.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise ValueError(f"file changed during hashing; retry on idle inputs: {path}")
    return {"path": relative, "kind": "file", "bytes": after.st_size, "sha256": digest}


def _tree_entries(root: Path) -> list[dict]:
    if root.is_file():
        return [_file_entry(root, root.name)]
    if not root.is_dir():
        raise ValueError("inventory root must be a regular file or directory")
    entries = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as scan:
            children = sorted(scan, key=lambda item: item.name)
        for child in children:
            path = Path(child.path)
            mode = path.lstat().st_mode
            if is_reparse_point(path) or stat.S_ISLNK(mode):
                raise ValueError(f"links/reparse points are not followed; inventory incomplete: {path}")
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(mode):
                entries.append({"path": relative, "kind": "directory"})
                pending.append(path)
            elif stat.S_ISREG(mode):
                entries.append(_file_entry(path, relative))
            else:
                raise ValueError(f"unsupported filesystem entry; inventory incomplete: {path}")
    return sorted(entries, key=lambda item: item["path"])


def _load_previous(path: Path, expected_hash: str, scope: dict, generator: dict) -> tuple[dict, str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_hash.lower():
        raise ValueError("previous snapshot SHA-256 changed; supply the original reviewed baseline")
    snapshot = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(snapshot, dict) or snapshot.get("version") != SNAPSHOT_VERSION:
        raise ValueError("previous file is not a supported inventory snapshot")
    if snapshot.get("scope") != scope:
        raise ValueError("previous snapshot covers a different root or record")
    if snapshot.get("generator") != generator:
        raise ValueError("inventory generator/settings changed; create and inspect a new baseline")
    payload = {key: snapshot[key] for key in ("version", "scope", "generator", "entries", "metadata")}
    if _fingerprint(payload) != snapshot.get("fingerprint"):
        raise ValueError("previous snapshot fingerprint does not match its contents")
    rows = snapshot["entries"]
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("path"), str) for row in rows):
        raise ValueError("previous snapshot entries are malformed")
    if len({row["path"] for row in rows}) != len(rows):
        raise ValueError("previous snapshot has duplicate paths")
    return snapshot, digest


def _changes(previous: dict, current: dict) -> list[dict]:
    old = {row["path"]: row for row in previous["entries"]}
    new = {row["path"]: row for row in current["entries"]}
    changes = []
    for path in sorted(old.keys() | new.keys()):
        if old.get(path) == new.get(path):
            continue
        kind = "added" if path not in old else "removed" if path not in new else "modified"
        changes.append({"path": path, "change": kind, "before": old.get(path), "after": new.get(path)})
    return changes


def _save(path: Path, value: dict) -> dict:
    raw = _serialized(value).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(raw)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def inventory(args: argparse.Namespace) -> dict:
    save_dir = args.save_dir.resolve()
    metadata = {}
    if args.command == "files":
        lexical = args.path.absolute()
        reject_reparse_chain(lexical, lexical.parent)
        root = lexical.resolve(strict=True)
        scope = {"mode": "files", "root": str(root), "recursive": True, "links": "refuse"}
        forbidden = [root] if root.is_dir() else []
    else:
        repository = args.repository_root.resolve(strict=True)
        record_id = validate_record_id(args.record_id)
        private = repository / "papers (private)"
        record = private / record_id
        scope = {"mode": "sources", "repository_root": str(repository), "record_id": record_id}
        if not save_dir.is_relative_to(private.resolve(strict=True)):
            raise ValueError("source inventories must be saved inside this repository's private directory")
        forbidden = [record / name for name in ("pdf", "html", "supplementary")]
    if any(save_dir.is_relative_to(path.resolve()) for path in forbidden):
        raise ValueError("--save-dir must be outside inventoried trees and archived source directories")
    generator = _generator(args.command)
    previous = previous_hash = None
    if args.previous:
        previous, previous_hash = _load_previous(args.previous, args.expect_previous_sha256, scope, generator)

    if args.command == "files":
        entries = _tree_entries(root)
    else:
        if __package__:
            from .extraction.discovery import discover_sources, source_fingerprint
        else:
            from extraction.discovery import discover_sources, source_fingerprint
        sources = discover_sources(repository, record_id)
        entries = sorted([source.as_dict() for source in sources], key=lambda item: item["path"])
        metadata = {"source_fingerprint": source_fingerprint(sources)}
    snapshot = {"version": SNAPSHOT_VERSION, "scope": scope, "generator": generator,
                "entries": entries, "metadata": metadata}
    snapshot["fingerprint"] = _fingerprint(snapshot)
    unchanged = previous is not None and previous["fingerprint"] == snapshot["fingerprint"]
    changes = _changes(previous, snapshot) if previous else []
    change_artifact = None
    if unchanged:
        artifact = {"path": str(args.previous.resolve()), "sha256": previous_hash,
                    "bytes": args.previous.stat().st_size}
    else:
        save_dir.mkdir(parents=True, exist_ok=True)
        run_dir = create_unique_private_directory(save_dir, "inventory-")
        artifact = _save(run_dir / "snapshot.json", snapshot)
        if previous:
            change_artifact = _save(run_dir / "changes.json", {
                "previous": {"path": str(args.previous.resolve()), "sha256": previous_hash},
                "current": artifact, "changes": changes,
                "metadata_changed": previous["metadata"] != metadata,
            })
    counts = Counter(row.get("kind", "file") for row in entries)
    summary = {
        "status": "unchanged" if unchanged else "changed" if previous else "baseline_created",
        "scope": scope,
        "counts": {"entries": len(entries), "files": counts["file"], "directories": counts["directory"],
                   "bytes": sum(row.get("bytes", 0) for row in entries)},
        "fingerprint": snapshot["fingerprint"], "snapshot": artifact, "snapshot_reused": unchanged,
        "change_counts": dict(Counter(row["change"] for row in changes)) if previous else None,
        "changes_report": change_artifact,
        "preview_filter": args.contains, "preview": [], "preview_omitted": 0,
    }
    rows = changes if previous else entries
    selected = [(index, row) for index, row in enumerate(rows) if args.contains is None or args.contains in row["path"]]
    summary["matching_preview_entries"] = len(selected)
    summary["preview_omitted"] = len(selected)
    for index, row in selected[:args.limit]:
        preview = {"path": row["path"], "pointer": f"/changes/{index}" if previous else f"/entries/{index}"}
        if previous:
            preview["change"] = row["change"]
        else:
            preview.update({key: row[key] for key in ("kind", "role", "bytes", "detected_format", "page_count") if key in row})
        trial = {**summary, "preview": [*summary["preview"], preview],
                 "preview_omitted": summary["preview_omitted"] - 1}
        if not _fits(trial, args.max_output_chars):
            break
        summary = trial
    if not _fits(summary, args.max_output_chars):
        raise ValueError("inventory summary metadata exceeds the budget; increase --max-output-chars")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("files", "sources"):
        sub = commands.add_parser(name)
        if name == "files":
            sub.add_argument("path", type=Path)
        else:
            sub.add_argument("record_id")
            sub.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])
        sub.add_argument("--save-dir", type=Path, required=True,
                         help="assigned private diagnostics directory, outside the inventoried tree")
        sub.add_argument("--previous", type=Path, help="compare with a saved snapshot")
        sub.add_argument("--expect-previous-sha256", help="pin the previous snapshot to its returned SHA-256")
        sub.add_argument("--contains", help="case-sensitive path substring for previews only; the complete inventory is saved")
        sub.add_argument("--limit", type=_bounded_int(1, 100), default=10)
        sub.add_argument("--max-output-chars", type=_bounded_int(2000, 32000), default=6000)
    args = parser.parse_args(argv)
    if bool(args.previous) != bool(args.expect_previous_sha256):
        parser.error("--previous and --expect-previous-sha256 must be supplied together")
    try:
        sys.stdout.write(_serialized(inventory(args)))
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        print(f"inventory failed: {str(exc)[:1500]}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
