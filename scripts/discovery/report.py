"""Machine- and human-readable discovery report rendering."""

from __future__ import annotations

import json
from pathlib import Path

from .models import DiscoveryReport


def _markdown(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_markdown(report: DiscoveryReport) -> str:
    lines = [
        "# PIP paper discovery report",
        "",
        f"- Outcome: `{report.status}`",
        f"- Search window: `{report.date_from.isoformat()}` through `{report.date_until.isoformat()}` (inclusive)",
        f"- Resolved UTC today: `{report.resolved_today.isoformat()}`",
        f"- Query configuration SHA-256: `{report.config_digest}`",
        "",
        "## Source status",
        "",
        "| Source | Complete | Requests | Cache hits | Retries | Results | Error |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in report.source_diagnostics:
        lines.append(
            f"| {_markdown(item.source)} | {'yes' if item.complete else 'no'} | {item.request_count} | "
            f"{item.cache_hits} | {item.retries} | {item.result_count} | {_markdown(item.error)} |"
        )
    lines.extend(
        [
            "",
            "## Candidates",
            "",
            "| Disposition | Title | DOI | Stage | Date | Discovered by | Enriched by | Score | Match reason | Possible records |",
            "| --- | --- | --- | --- | --- | --- | --- | ---: | --- | --- |",
        ]
    )
    for candidate in report.candidates:
        reason = "; ".join(
            f"{item.query}/{item.field} (+{item.score})" for item in candidate.evidence
        )
        lines.append(
            f"| {_markdown(candidate.disposition)} | {_markdown(candidate.title)} | "
            f"{_markdown(candidate.doi)} | {_markdown(candidate.publication_stage)} | "
            f"{candidate.canonical_date.isoformat() if candidate.canonical_date else ''} | "
            f"{_markdown(', '.join(sorted(candidate.discovered_by)))} | "
            f"{_markdown(', '.join(sorted(candidate.enriched_by)))} | {candidate.score} | "
            f"{_markdown(reason)} | {_markdown(', '.join(candidate.matched_record_ids))} |"
        )
        for warning in candidate.warnings:
            lines.append(f"  - **{_markdown(candidate.stable_identifier)}:** {_markdown(warning)}")
    if report.generated_records:
        lines.extend(["", "## Generated records", ""])
        for record_id, identifier in sorted(report.generated_records.items()):
            lines.append(f"- `{record_id}`: `{_markdown(identifier)}`")
    if report.warnings:
        lines.extend(["", "## Run warnings", ""])
        lines.extend(f"- {_markdown(item)}" for item in report.warnings)
    lines.append("")
    return "\n".join(lines)


def write_reports(report: DiscoveryReport, json_path: Path, markdown_path: Path) -> None:
    for path in (json_path, markdown_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    json_temporary = json_path.with_name(json_path.name + ".tmp")
    markdown_temporary = markdown_path.with_name(markdown_path.name + ".tmp")
    json_temporary.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    markdown_temporary.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    json_temporary.replace(json_path)
    markdown_temporary.replace(markdown_path)
