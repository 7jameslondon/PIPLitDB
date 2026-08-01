#!/usr/bin/env python3
"""Measure OpenAlex query recall for a fixed window against known database DOIs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import quote

import yaml

from discovery.http import HttpClient, HttpFailure
from discovery.models import SourceDiagnostics
from discovery.normalize import normalize_doi


ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--until", dest="date_until", required=True)
    parser.add_argument("--discovery-report", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / ".cache" / "discovery" / "openalex-validation",
    )
    return parser.parse_args(argv)


def _markdown(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _render_markdown(payload: dict) -> str:
    lines = [
        "# OpenAlex historical-window validation",
        "",
        f"- Window: `{payload['date_from']}` through `{payload['date_until']}` (inclusive)",
        f"- Known database records checked: `{payload['known_records_checked']}`",
        f"- OpenAlex-indexed database records in window: `{payload['expected_count']}`",
        f"- Recovered by configured searches: `{payload['recovered_count']}`",
        f"- Exact-DOI recall: `{payload['recall'] if payload['recall'] is not None else 'n/a'}`",
        f"- Actionable novel results: `{payload['actionable_count']}`",
        "",
        "## Expected known papers",
        "",
        "| Recovered | PIP LitDB ID | DOI | OpenAlex date | Title |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in payload["expected_papers"]:
        lines.append(
            f"| {'yes' if item['recovered'] else 'no'} | {item['pip_litdb_id']} | "
            f"{_markdown(item['doi'])} | {item['openalex_date']} | {_markdown(item['title'])} |"
        )
    if not payload["expected_papers"]:
        lines.append("| n/a | n/a | n/a | n/a | No eligible known papers |")
    lines.extend(
        [
            "",
            "## Potential additions",
            "",
            "| Disposition | Score | Date | DOI | Title | Match evidence |",
            "| --- | ---: | --- | --- | --- | --- |",
        ]
    )
    for item in payload["actionable_candidates"]:
        evidence = "; ".join(
            f"{value.get('query')}/{value.get('field')} (+{value.get('score')})"
            for value in item["evidence"]
        )
        lines.append(
            f"| {item['disposition']} | {item['score']} | {item['canonical_date']} | "
            f"{_markdown(item['doi'])} | {_markdown(item['title'])} | {_markdown(evidence)} |"
        )
    if not payload["actionable_candidates"]:
        lines.append("| n/a | 0 | n/a | n/a | No potential additions | n/a |")
    if payload["lookup_failures"]:
        lines.extend(["", "## OpenAlex DOI lookup failures", ""])
        for item in payload["lookup_failures"]:
            lines.append(
                f"- `{item['pip_litdb_id']}` / `{item['doi']}`: {_markdown(item['error'])}"
            )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        start = date.fromisoformat(args.date_from)
        end = date.fromisoformat(args.date_until)
        if end < start:
            raise ValueError("until must not precede from")
        api_key = os.environ.get("OPENALEX_API_KEY", "")
        if not api_key:
            raise ValueError("OPENALEX_API_KEY is required")
        discovery = json.loads(args.discovery_report.read_text(encoding="utf-8"))
        candidates = discovery.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError("discovery report is missing candidates")

        discovered_dois = {
            doi
            for item in candidates
            if isinstance(item, dict) and (doi := normalize_doi(item.get("doi")))
        }
        diagnostics = SourceDiagnostics("openalex_known_doi_lookup")
        client = HttpClient(
            "openalex_known_doi_lookup",
            args.cache_dir,
            diagnostics,
            max_requests=100,
            minimum_interval=0.1,
        )
        expected = []
        lookup_failures = []
        known_records_checked = 0
        for path in sorted((ROOT / "database" / "records").glob("[0-9][0-9][0-9][0-9][0-9].yaml")):
            record = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                continue
            record_year = int(record.get("publication_year") or 0)
            if not start.year <= record_year <= end.year:
                continue
            doi = normalize_doi(record.get("doi"))
            if not doi:
                continue
            known_records_checked += 1
            endpoint = "https://api.openalex.org/works/https://doi.org/" + quote(doi, safe="/")
            try:
                work = client.get_json(
                    endpoint,
                    {
                        "api_key": api_key,
                        "select": "id,doi,display_name,publication_date",
                    },
                )
                raw_date = work.get("publication_date") if isinstance(work, dict) else None
                openalex_date = date.fromisoformat(str(raw_date))
            except (HttpFailure, TypeError, ValueError) as exc:
                lookup_failures.append(
                    {
                        "pip_litdb_id": path.stem,
                        "doi": doi,
                        "error": str(exc)[:300],
                    }
                )
                continue
            if start <= openalex_date <= end:
                expected.append(
                    {
                        "pip_litdb_id": path.stem,
                        "doi": doi,
                        "title": str(record.get("title") or ""),
                        "openalex_id": str(work.get("id") or ""),
                        "openalex_date": openalex_date.isoformat(),
                        "recovered": doi in discovered_dois,
                    }
                )

        expected.sort(key=lambda item: (item["openalex_date"], item["pip_litdb_id"]))
        recovered_count = sum(item["recovered"] for item in expected)
        actionable = []
        for item in candidates:
            if not isinstance(item, dict) or item.get("disposition") not in {"new", "related_version"}:
                continue
            actionable.append(
                {
                    "title": str(item.get("title") or ""),
                    "doi": normalize_doi(item.get("doi")),
                    "canonical_date": item.get("canonical_date"),
                    "score": int(item.get("score") or 0),
                    "disposition": item.get("disposition"),
                    "evidence": item.get("evidence") if isinstance(item.get("evidence"), list) else [],
                    "openalex_id": (item.get("discovered_by") or {}).get("openalex"),
                    "matched_record_ids": item.get("matched_record_ids") or [],
                }
            )
        actionable.sort(key=lambda item: (-item["score"], str(item["canonical_date"]), item["title"]))
        payload = {
            "format_version": 1,
            "measurement": "retrospective_current_openalex_index_recovery",
            "date_from": start.isoformat(),
            "date_until": end.isoformat(),
            "known_records_checked": known_records_checked,
            "expected_count": len(expected),
            "recovered_count": recovered_count,
            "recall": recovered_count / len(expected) if expected else None,
            "missed_ids": [item["pip_litdb_id"] for item in expected if not item["recovered"]],
            "expected_papers": expected,
            "discovery_status": discovery.get("status"),
            "discovery_counts": discovery.get("counts"),
            "disposition_counts": dict(
                sorted(Counter(str(item.get("disposition")) for item in candidates if isinstance(item, dict)).items())
            ),
            "actionable_count": len(actionable),
            "actionable_candidates": actionable,
            "lookup_failures": lookup_failures,
            "lookup_requests": diagnostics.request_count,
            "lookup_retries": diagnostics.retries,
        }
        markdown = _render_markdown(payload)
        for output in (args.json_output, args.markdown_output):
            output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        args.markdown_output.write_text(markdown, encoding="utf-8")
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with Path(summary_path).open("a", encoding="utf-8") as summary:
                summary.write(markdown)
        print("OPENALEX_VALIDATION=" + json.dumps(payload, separators=(",", ":")))
        return 0
    except (OSError, TypeError, ValueError, yaml.YAMLError, json.JSONDecodeError) as exc:
        print(f"OpenAlex validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
