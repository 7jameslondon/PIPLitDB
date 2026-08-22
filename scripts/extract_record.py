#!/usr/bin/env python3
"""Create one private staged extraction candidate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from extraction.pipeline import ExtractionError, extract_record, inventory_record


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record_id", help="five-digit PIP LitDB record ID")
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the parent of scripts/)",
    )
    parser.add_argument("--run-id", help="unique staged-run name")
    parser.add_argument("--override", type=Path, help="private override YAML")
    parser.add_argument("--dpi", type=int, default=300, help="PDF crop DPI")
    parser.add_argument(
        "--inventory-only",
        action="store_true",
        help="read and report source inventory without writing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.inventory_only:
            result = inventory_record(args.repository_root, args.record_id)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if not args.run_id:
            _parser().error("--run-id is required unless --inventory-only is used")
        result = extract_record(
            args.repository_root,
            args.record_id,
            run_id=args.run_id,
            override_path=args.override,
            dpi=args.dpi,
        )
    except KeyboardInterrupt:
        return 130
    except (ExtractionError, FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"extraction failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "record_id": result.record_id,
                "run_id": result.run_id,
                "status": result.status,
                "finding_count": result.finding_count,
                "run_root": str(result.run_root),
                "extraction": str(result.extraction_root),
                "diagnostic": str(result.diagnostic_root),
                "source_fingerprint": result.source_fingerprint,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
