#!/usr/bin/env python3
"""Resolve and verify GitHub's synthetic pull-request merge ref."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass(frozen=True)
class MergeIdentity:
    commit_sha: str
    tree_sha: str
    base_sha: str
    head_sha: str

    @property
    def content_key(self) -> tuple[str, str, str]:
        """Identify validated content independently of merge-commit metadata."""

        return (self.base_sha, self.head_sha, self.tree_sha)


class MergeResolutionError(RuntimeError):
    """Raised when the expected pull-request merge content cannot be resolved."""


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _git_value(root: Path, *arguments: str) -> str:
    result = _git(root, *arguments)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise MergeResolutionError(
            f"git {' '.join(arguments)} failed: {detail or result.returncode}"
        )
    return result.stdout.strip()


def resolve_pull_request_merge(
    root: Path | str,
    *,
    remote: str,
    pr_number: int,
    expected_base_sha: str,
    expected_head_sha: str,
    max_attempts: int = 8,
    delay_seconds: float = 2.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> MergeIdentity:
    """Fetch the PR merge ref and require its ordered parents to match the event."""

    if pr_number < 1:
        raise ValueError("pr_number must be positive")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if delay_seconds < 0:
        raise ValueError("delay_seconds cannot be negative")

    root_path = Path(root).resolve()
    base_sha = expected_base_sha.strip().lower()
    head_sha = expected_head_sha.strip().lower()
    merge_ref = f"refs/pull/{pr_number}/merge"
    local_ref = f"refs/pip-litdb/pull/{pr_number}/merge"
    last_detail = f"{merge_ref} was not available"

    for attempt in range(1, max_attempts + 1):
        fetch = _git(
            root_path,
            "fetch",
            "--no-tags",
            "--force",
            remote,
            f"{merge_ref}:{local_ref}",
        )
        if fetch.returncode == 0:
            try:
                commit_sha = _git_value(
                    root_path, "rev-parse", "--verify", f"{local_ref}^{{commit}}"
                ).lower()
                parents = tuple(
                    _git_value(root_path, "show", "-s", "--format=%P", commit_sha)
                    .lower()
                    .split()
                )
                if parents == (base_sha, head_sha):
                    tree_sha = _git_value(
                        root_path,
                        "rev-parse",
                        "--verify",
                        f"{commit_sha}^{{tree}}",
                    ).lower()
                    return MergeIdentity(
                        commit_sha=commit_sha,
                        tree_sha=tree_sha,
                        base_sha=base_sha,
                        head_sha=head_sha,
                    )
                last_detail = (
                    f"{commit_sha} has parents {' '.join(parents) or '<none>'}; "
                    f"expected {base_sha} {head_sha}"
                )
            except MergeResolutionError as error:
                last_detail = str(error)
        else:
            last_detail = fetch.stderr.strip() or fetch.stdout.strip() or last_detail

        if attempt < max_attempts:
            sleeper(delay_seconds * attempt)

    raise MergeResolutionError(
        f"Could not resolve {merge_ref} with the expected parents after "
        f"{max_attempts} attempts: {last_detail}"
    )


def write_github_outputs(path: Path | str, identity: MergeIdentity) -> None:
    output_path = Path(path)
    with output_path.open("a", encoding="utf-8", newline="\n") as output:
        for name, value in asdict(identity).items():
            output.write(f"{name}={value}\n")


def _annotation_escape(value: str) -> str:
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--expected-base", required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--max-attempts", type=int, default=8)
    parser.add_argument("--delay-seconds", type=float, default=2.0)
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        identity = resolve_pull_request_merge(
            args.root,
            remote=args.remote,
            pr_number=args.pr_number,
            expected_base_sha=args.expected_base,
            expected_head_sha=args.expected_head,
            max_attempts=args.max_attempts,
            delay_seconds=args.delay_seconds,
        )
    except (MergeResolutionError, ValueError) as error:
        print(
            f"::error title=Cannot resolve pull request merge::"
            f"{_annotation_escape(str(error))}"
        )
        return 1

    github_output = args.github_output or (
        Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None
    )
    if github_output is not None:
        write_github_outputs(github_output, identity)
    print(json.dumps(asdict(identity), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
