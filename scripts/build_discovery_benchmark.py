#!/usr/bin/env python3
"""Bootstrap an exact-date benchmark candidate for manual review."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote

import yaml

from discovery.http import HttpClient, HttpFailure
from discovery.models import SourceDiagnostics
from discovery.normalize import date_from_parts, normalize_doi


ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="date_from", default="2018-01-01")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", type=Path, required=True, help="review candidate output; committed manifest is never overwritten implicitly")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".cache" / "discovery" / "benchmark-bootstrap")
    parser.add_argument("--offline", action="store_true")
    return parser.parse_args(argv)


def _crossref_date(message: dict) -> tuple[date | None, str]:
    for field_name, kind in (("published-online", "online"), ("issued", "issued"), ("published-print", "print")):
        value = message.get(field_name)
        parts = value.get("date-parts") if isinstance(value, dict) else None
        first = parts[0] if isinstance(parts, list) and parts else None
        parsed = date_from_parts(first)
        if parsed:
            return parsed, kind
    return None, ""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        start = date.fromisoformat(args.date_from)
        as_of = date.fromisoformat(args.as_of)
        if not args.offline and not os.environ.get("DISCOVERY_CONTACT_EMAIL"):
            raise ValueError("DISCOVERY_CONTACT_EMAIL is required for live benchmark bootstrap")
        diagnostics = SourceDiagnostics("crossref_benchmark")
        client = HttpClient("crossref_benchmark", args.cache_dir, diagnostics, offline=args.offline)
        papers = []
        for path in sorted((ROOT / "database" / "records").glob("[0-9][0-9][0-9][0-9][0-9].yaml")):
            record = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or int(record.get("publication_year") or 0) < start.year:
                continue
            doi = normalize_doi(record.get("doi"))
            if not doi:
                continue
            payload = client.get_json(
                f"https://api.crossref.org/works/{quote(doi, safe='')}",
                {"mailto": os.environ.get("DISCOVERY_CONTACT_EMAIL")},
            )
            message = payload.get("message") if isinstance(payload, dict) else None
            if not isinstance(message, dict):
                raise HttpFailure(f"Crossref has no work metadata for {doi}")
            canonical_date, date_kind = _crossref_date(message)
            if canonical_date is None or not start <= canonical_date <= as_of:
                continue
            if canonical_date.year <= 2022:
                split = "development"
            elif canonical_date.year <= 2024:
                split = "validation"
            else:
                split = "holdout"
            papers.append(
                {
                    "pip_litdb_id": path.stem,
                    "doi": doi,
                    "canonical_date": canonical_date.isoformat(),
                    "date_kind": date_kind,
                    "date_source": "Crossref (bootstrap; manual review required)",
                    "publication_stage": record.get("publication_stage"),
                    "union_eligible": True,
                    "source_eligibility": {"crossref": doi},
                    "evaluation_split": split,
                    "notes": "Verify against PubMed/OpenAlex and direct preprint metadata before recording baseline.",
                }
            )
        output = {
            "version": 1,
            "as_of": as_of.isoformat(),
            "status": "needs_manual_review",
            "generated_record_count": len(papers),
            "database_records_from_start_year": sum(
                1
                for path in (ROOT / "database" / "records").glob("*.yaml")
                if isinstance((record := yaml.safe_load(path.read_text(encoding="utf-8"))), dict)
                and int(record.get("publication_year") or 0) >= start.year
            ),
            "papers": papers,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(yaml.safe_dump(output, sort_keys=False, allow_unicode=True), encoding="utf-8")
        print(f"Wrote {len(papers)} benchmark entries to {args.output}; manual review is required.")
        return 0
    except (OSError, HttpFailure, TypeError, ValueError, yaml.YAMLError) as exc:
        print(f"Benchmark bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
