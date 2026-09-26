#!/usr/bin/env python3
"""Finalize and automatically approve one extraction under standing policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from extraction.finalization import finalize_extraction
from extraction.promotion import PromotionError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record_id", help="five-digit PIP LitDB record ID")
    parser.add_argument("--run-id", required=True, help="final reviewed staged run")
    parser.add_argument(
        "--repro-run-id",
        required=True,
        help="distinct clean run that exactly reproduces the canonical extraction",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an approved live extraction and archive its prior revision",
    )
    parser.add_argument(
        "--keep-staging",
        action="store_true",
        help="verify cleanup eligibility but retain staging for diagnosis",
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
        result = finalize_extraction(
            args.repository_root,
            args.record_id,
            run_id=args.run_id,
            reproducibility_run_id=args.repro_run_id,
            replace=args.replace,
            remove_staging=not args.keep_staging,
        )
    except KeyboardInterrupt:
        return 130
    except (PromotionError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"finalization failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
