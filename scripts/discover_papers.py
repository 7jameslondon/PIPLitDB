#!/usr/bin/env python3
"""Search configured sources for new PIP papers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from discovery.config import ALLOWED_SOURCES, load_exclusions, load_queries
from discovery.dates import resolve_range
from discovery.deduplicate import load_database
from discovery.pipeline import run_discovery
from discovery.records import write_records
from discovery.report import write_reports


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="date_from")
    parser.add_argument("--until", dest="date_until")
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--today", help=argparse.SUPPRESS)
    parser.add_argument("--source", action="append", choices=sorted(ALLOWED_SOURCES))
    parser.add_argument("--query-file", type=Path, default=REPOSITORY_ROOT / "discovery" / "queries.yaml")
    parser.add_argument("--exclusions", type=Path, default=REPOSITORY_ROOT / "discovery" / "exclusions.yaml")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="search and report without changing records (default)")
    mode.add_argument("--write-records", action="store_true", help="generate validator-clean candidate records")
    parser.add_argument("--cache-dir", type=Path, default=REPOSITORY_ROOT / ".cache" / "discovery")
    parser.add_argument("--offline", action="store_true", help="prohibit network requests and require cached responses")
    parser.add_argument("--allow-partial", action="store_true", help="report successful sources when another source fails")
    parser.add_argument("--json-report", type=Path)
    parser.add_argument("--markdown-report", type=Path)
    parser.add_argument("--base", default="HEAD", help="base revision for history-aware ID allocation")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        start, end, today = resolve_range(
            args.date_from,
            args.date_until,
            months=args.months,
            today=args.today,
        )
        if args.write_records and (args.source or args.allow_partial):
            raise ValueError("--source and --allow-partial always imply dry-run and cannot be used with --write-records")
        if args.source and len(args.source) != len(set(args.source)):
            raise ValueError("--source values must be unique")
        config = load_queries(args.query_file)
        exclusions = load_exclusions(args.exclusions)
        database = load_database(REPOSITORY_ROOT / "database" / "records")
        report = run_discovery(
            config=config,
            exclusions=exclusions,
            database=database,
            start=start,
            end=end,
            resolved_today=today,
            cache_dir=args.cache_dir,
            offline=args.offline,
            sources=args.source,
            allow_partial=args.allow_partial,
        )
        if args.write_records:
            if report.status != "candidates":
                if report.status in {"failed", "partial"}:
                    raise ValueError("publication is disabled because discovery was incomplete")
            else:
                report.generated_records = write_records(
                    REPOSITORY_ROOT,
                    report.candidates,
                    base=args.base,
                )
        stem = f"discovery-{start.isoformat()}-{end.isoformat()}"
        json_path = args.json_report or REPOSITORY_ROOT / "artifacts" / "discovery" / f"{stem}.json"
        markdown_path = args.markdown_report or REPOSITORY_ROOT / "artifacts" / "discovery" / f"{stem}.md"
        write_reports(report, json_path, markdown_path)
        print(
            f"Discovery outcome: {report.status}; {len(report.candidates)} normalized candidate(s).\n"
            f"JSON: {json_path}\nMarkdown: {markdown_path}"
        )
        return 1 if report.status == "failed" else 0
    except (OSError, RuntimeError, ValueError) as exc:
        failure_path = args.json_report or REPOSITORY_ROOT / "artifacts" / "discovery" / "discovery-failed.json"
        failure_path.parent.mkdir(parents=True, exist_ok=True)
        failure_path.write_text(
            json.dumps(
                {"format_version": 1, "status": "failed", "error": str(exc)[:1000]},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Discovery failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
