#!/usr/bin/env python3
"""Run trusted discovery and create a bounded data-only publication handoff."""

from __future__ import annotations

import os
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from discovery.automation import (
    AutomationFailure,
    AutomationInventory,
    GitHubApi,
    inventory,
    make_handoff,
    write_handoff,
)
from discovery.config import Exclusion, load_exclusions, load_queries
from discovery.dates import resolve_range
from discovery.deduplicate import load_database
from discovery.pipeline import run_discovery
from discovery.report import write_reports
from discovery.sources import REQUIRED_DISCOVERY_SOURCES


ROOT = Path(__file__).resolve().parents[1]


def _boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if raw not in {"true", "false"}:
        raise ValueError(f"{name} must be the literal true or false")
    return raw == "true"


def _output(name: str, value: str) -> None:
    destination = os.environ.get("GITHUB_OUTPUT")
    if destination:
        with Path(destination).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{name}={value}\n")


def main() -> int:
    try:
        source_input = os.environ.get("DISCOVERY_SOURCE", "all")
        if source_input not in {"all", *REQUIRED_DISCOVERY_SOURCES}:
            raise ValueError("DISCOVERY_SOURCE is not a declared source choice")
        selected = None if source_input == "all" else [source_input]
        allow_partial = _boolean("DISCOVERY_ALLOW_PARTIAL", False)
        dry_run = _boolean("DISCOVERY_DRY_RUN", True)
        start, end, today = resolve_range(
            os.environ.get("DISCOVERY_FROM") or None,
            os.environ.get("DISCOVERY_UNTIL") or None,
        )
        base_sha = os.environ.get("DISCOVERY_BASE_SHA", "")
        if not base_sha:
            completed = subprocess.run(
                ["git", "-c", f"safe.directory={ROOT.as_posix()}", "rev-parse", "HEAD"],
                cwd=ROOT,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            base_sha = completed.stdout.strip()
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GITHUB_TOKEN", "")
        automation_inventory = (
            inventory(GitHubApi(repository, token))
            if repository and token
            else AutomationInventory((), ())
        )
        config = load_queries(ROOT / "discovery" / "queries.yaml")
        exclusions = load_exclusions(ROOT / "discovery" / "exclusions.yaml")
        exclusions.extend(
            Exclusion(identifier, "pending in an open automated discovery PR", today)
            for identifier in automation_inventory.pending_identifiers
        )
        report = run_discovery(
            config=config,
            exclusions=exclusions,
            database=load_database(ROOT / "database" / "records"),
            start=start,
            end=end,
            resolved_today=today,
            cache_dir=ROOT / ".cache" / "discovery",
            sources=selected,
            allow_partial=allow_partial,
        )
        artifact_dir = ROOT / "artifacts" / "discovery"
        write_reports(report, artifact_dir / "report.json", artifact_dir / "report.md")
        requested_publish = not dry_run and selected is None and not allow_partial
        handoff = make_handoff(
            report,
            base_sha=base_sha,
            repository=repository,
            default_branch=os.environ.get("DISCOVERY_DEFAULT_BRANCH", "main"),
            publication_allowed=requested_publish,
            workflow_run_url=os.environ.get("DISCOVERY_RUN_URL", ""),
            automation_inventory=automation_inventory,
        )
        checksum = write_handoff(artifact_dir / "handoff.json", handoff)
        has_work = bool(handoff["candidates"] or handoff["resume_prs"] or handoff["orphan_branches"])
        _output("publication_allowed", "true" if handoff["publication_allowed"] else "false")
        _output("has_publish_work", "true" if has_work else "false")
        _output("handoff_sha256", checksum)
        _output("base_sha", base_sha)
        print(f"Discovery {report.status}; publication_allowed={handoff['publication_allowed']}; has_publish_work={has_work}")
        return 1 if report.status == "failed" else 0
    except (AutomationFailure, OSError, RuntimeError, ValueError) as exc:
        artifact_dir = ROOT / "artifacts" / "discovery"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        failure = {"format_version": 1, "status": "failed", "error": str(exc)[:1000]}
        (artifact_dir / "failure.json").write_text(
            json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (artifact_dir / "failure.md").write_text(
            "# PIP paper discovery failure\n\nThe trusted discovery job failed before publication.\n\n"
            f"`{str(exc)[:1000].replace('`', "'")}`\n",
            encoding="utf-8",
        )
        print(f"Handoff preparation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
