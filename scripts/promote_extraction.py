#!/usr/bin/env python3
"""Manually promote one extraction after explicit user approval (legacy path)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from extraction.promotion import (
    EXPLICIT_USER_APPROVAL,
    PromotionError,
    promote_extraction,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record_id", help="five-digit PIP LitDB record ID")
    parser.add_argument("--run-id", required=True, help="reviewed staged-run name")
    parser.add_argument(
        "--accept-finding",
        action="append",
        default=[],
        metavar="CODE",
        help="accept one current validation finding code (repeat for every code)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an approved live extraction and archive its prior revision",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the parent of scripts/)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = promote_extraction(
            args.repository_root,
            args.record_id,
            run_id=args.run_id,
            accepted_findings=args.accept_finding,
            approval_mode=EXPLICIT_USER_APPROVAL,
            replace=args.replace,
        )
    except KeyboardInterrupt:
        return 130
    except (PromotionError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"promotion failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
