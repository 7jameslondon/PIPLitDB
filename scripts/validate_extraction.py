#!/usr/bin/env python3
"""Independently validate an existing staged extraction candidate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from extraction.validation import validate_candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path, help="run directory containing extraction/")
    parser.add_argument("--expected-title", required=True)
    parser.add_argument("--main-assets", type=int)
    parser.add_argument("--tables", type=int)
    parser.add_argument("--supplement-figures", type=int)
    parser.add_argument("--equations", type=int)
    parser.add_argument("--presentation-embedded-files", type=int)
    args = parser.parse_args(argv)
    expected_counts = {
        key: value
        for key, value in {
            "main_assets": args.main_assets,
            "tables": args.tables,
            "supplement_figures": args.supplement_figures,
            "equations": args.equations,
            "presentation_embedded_files": args.presentation_embedded_files,
        }.items()
        if value is not None
    }
    try:
        report = validate_candidate(
            args.candidate / "extraction",
            args.candidate / "extraction_diagnostic",
            expected_title=args.expected_title,
            expected_counts=expected_counts or None,
        )
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"validation failed to run: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
