#!/usr/bin/env python3
"""Safely remove staging history for one fully approved extraction."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from extraction.promotion import PromotionError, cleanup_approved_staging


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record_id", help="five-digit PIP LitDB record ID")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="verify cleanup eligibility without deleting staging",
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
        result = cleanup_approved_staging(
            args.repository_root,
            args.record_id,
            remove=not args.check_only,
        )
    except KeyboardInterrupt:
        return 130
    except (PromotionError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"staging cleanup failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
