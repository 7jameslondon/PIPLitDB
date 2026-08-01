#!/usr/bin/env python3
"""Publish a validated discovery handoff as an idempotent review PR."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from discovery.automation import AutomationFailure, GitHubApi, publish_handoff, read_handoff


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    try:
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("DISCOVERY_PUBLISH_TOKEN", "")
        if not token:
            raise AutomationFailure(
                "DISCOVERY_PUBLISH_TOKEN is required so bot-created PR events can trigger trusted validation"
            )
        checksum = os.environ.get("DISCOVERY_HANDOFF_SHA256", "")
        handoff_path = Path(os.environ.get("DISCOVERY_HANDOFF", ROOT / "artifacts" / "discovery" / "handoff.json"))
        handoff = read_handoff(handoff_path, checksum, expected_repository=repository)
        handled = publish_handoff(ROOT, handoff, GitHubApi(repository, token))
        print("Handled automated discovery PR(s): " + (", ".join(f"#{number}" for number in handled) or "none"))
        return 0
    except (AutomationFailure, OSError, RuntimeError, ValueError) as exc:
        print(f"Publication failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
