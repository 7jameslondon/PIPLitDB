"""Versioned benchmark manifest loading and retrospective evaluation."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any, Iterable

import yaml

from .dates import DateWindow
from .models import Candidate
from .normalize import normalize_doi


SPLITS = frozenset({"development", "validation", "holdout"})


@dataclass(frozen=True)
class BenchmarkPaper:
    record_id: str
    doi: str
    canonical_date: date
    date_kind: str
    date_source: str
    publication_stage: str
    union_eligible: bool
    source_eligibility: dict[str, str | None]
    evaluation_split: str
    notes: str | None = None


@dataclass
class WindowResult:
    window: DateWindow
    hidden_count: int
    recovered_count: int
    recovered_ids: list[str]
    missed_ids: list[str]
    novel_identifiers: list[str]
    source_recovered: dict[str, int]
    source_eligible: dict[str, int]
    split_counts: dict[str, dict[str, int]]
    misses: list[dict[str, Any]]
    date_disagreements: list[dict[str, str]]
    full_database_existing: int

    @property
    def recall(self) -> float | None:
        return self.recovered_count / self.hidden_count if self.hidden_count else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": {
                "start": self.window.start.isoformat(),
                "end": self.window.end.isoformat(),
                "kind": self.window.kind,
            },
            "hidden_count": self.hidden_count,
            "recovered_count": self.recovered_count,
            "recall": self.recall,
            "recovered_ids": self.recovered_ids,
            "missed_ids": self.missed_ids,
            "novel_identifiers": self.novel_identifiers,
            "source_recovered": self.source_recovered,
            "source_eligible": self.source_eligible,
            "split_counts": self.split_counts,
            "misses": self.misses,
            "date_disagreements": self.date_disagreements,
            "full_database_existing": self.full_database_existing,
        }


def load_manifest(path: Path, *, allow_unrecorded: bool = False) -> tuple[date | None, list[BenchmarkPaper]]:
    try:
        root = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load benchmark manifest {path}: {exc}") from exc
    if not isinstance(root, dict) or root.get("version") != 1 or not isinstance(root.get("papers"), list):
        raise ValueError("benchmark manifest must have version 1 and a papers list")
    status = root.get("status", "recorded")
    if status != "recorded" and not allow_unrecorded:
        raise ValueError("benchmark manifest has not been recorded and reviewed")
    raw_as_of = root.get("as_of")
    as_of = raw_as_of if isinstance(raw_as_of, date) else date.fromisoformat(str(raw_as_of)) if raw_as_of else None
    papers: list[BenchmarkPaper] = []
    ids: set[str] = set()
    dois: set[str] = set()
    for index, raw in enumerate(root["papers"]):
        if not isinstance(raw, dict):
            raise ValueError(f"benchmark paper {index} must be a mapping")
        record_id = str(raw.get("pip_litdb_id") or "")
        doi = normalize_doi(raw.get("doi"))
        split = raw.get("evaluation_split")
        eligibility = raw.get("source_eligibility")
        if len(record_id) != 5 or not record_id.isdigit() or record_id in ids:
            raise ValueError(f"benchmark paper {index} has an invalid or duplicate PIP LitDB ID")
        if not doi or doi in dois:
            raise ValueError(f"benchmark paper {index} has an invalid or duplicate DOI")
        if split not in SPLITS:
            raise ValueError(f"benchmark paper {index} has an invalid evaluation_split")
        if not isinstance(eligibility, dict) or any(
            not isinstance(source, str) or (identifier is not None and not isinstance(identifier, str))
            for source, identifier in eligibility.items()
        ):
            raise ValueError(f"benchmark paper {index} has invalid source_eligibility")
        try:
            raw_date = raw.get("canonical_date")
            canonical_date = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date))
        except ValueError as exc:
            raise ValueError(f"benchmark paper {index} has an invalid canonical_date") from exc
        paper = BenchmarkPaper(
            record_id,
            doi,
            canonical_date,
            str(raw.get("date_kind") or ""),
            str(raw.get("date_source") or ""),
            str(raw.get("publication_stage") or ""),
            bool(raw.get("union_eligible")),
            dict(eligibility),
            str(split),
            raw.get("notes"),
        )
        if not paper.date_kind or not paper.date_source or paper.publication_stage not in {"preprint", "publication"}:
            raise ValueError(f"benchmark paper {index} lacks date/stage metadata")
        ids.add(record_id)
        dois.add(doi)
        papers.append(paper)
    return as_of, papers


def replay_candidates(path: Path, window: DateWindow) -> list[Candidate] | None:
    if not path.is_file():
        return None
    candidates: list[Candidate] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid replay JSON on line {line_number}") from exc
        if value.get("window") == window.key:
            candidates.append(Candidate.from_dict(value["candidate"]))
    return candidates or None


def evaluate_window(
    window: DateWindow,
    papers: Iterable[BenchmarkPaper],
    candidates: Iterable[Candidate],
    *,
    sources: Iterable[str],
    full_database_existing: int,
) -> WindowResult:
    hidden = [
        paper
        for paper in papers
        if paper.union_eligible and window.start <= paper.canonical_date <= window.end
    ]
    candidate_list = list(candidates)
    by_doi = {candidate.doi: candidate for candidate in candidate_list if candidate.doi}
    recovered = [paper for paper in hidden if paper.doi in by_doi]
    recovered_dois = {paper.doi for paper in recovered}
    source_recovered = {
        source: sum(
            paper.doi in by_doi and source in by_doi[paper.doi].discovered_by
            for paper in hidden
            if source in paper.source_eligibility
        )
        for source in sources
    }
    source_eligible = {
        source: sum(source in paper.source_eligibility for paper in hidden)
        for source in sources
    }
    split_counts = {
        split: {
            "hidden": sum(paper.evaluation_split == split for paper in hidden),
            "recovered": sum(
                paper.evaluation_split == split and paper.doi in recovered_dois for paper in hidden
            ),
        }
        for split in sorted(SPLITS)
    }
    misses = [
        {
            "pip_litdb_id": paper.record_id,
            "doi": paper.doi,
            "reason": "not_recovered_from_current_indexes",
            "expected_sources": sorted(paper.source_eligibility),
            "notes": paper.notes,
        }
        for paper in hidden
        if paper.doi not in recovered_dois
    ]
    date_disagreements = []
    for paper in recovered:
        asserted = by_doi[paper.doi].canonical_date
        if asserted and asserted != paper.canonical_date:
            date_disagreements.append(
                {
                    "pip_litdb_id": paper.record_id,
                    "doi": paper.doi,
                    "benchmark_date": paper.canonical_date.isoformat(),
                    "source_selected_date": asserted.isoformat(),
                }
            )
    benchmark_dois = {paper.doi for paper in papers}
    novel = sorted(
        candidate.stable_identifier
        for candidate in candidate_list
        if not candidate.doi or candidate.doi not in benchmark_dois
    )
    return WindowResult(
        window,
        len(hidden),
        len(recovered),
        sorted(paper.record_id for paper in recovered),
        sorted(paper.record_id for paper in hidden if paper.doi not in recovered_dois),
        novel,
        source_recovered,
        source_eligible,
        split_counts,
        misses,
        date_disagreements,
        full_database_existing,
    )


def render_backtest_reports(
    results: list[WindowResult],
    *,
    as_of: date,
    config_digest: str,
    run_id: str,
) -> tuple[dict[str, Any], str, str]:
    completed = [item for item in results if item.window.kind == "completed"]
    hidden = sum(item.hidden_count for item in completed)
    recovered = sum(item.recovered_count for item in completed)
    micro = recovered / hidden if hidden else None
    recalls = [item.recall for item in completed if item.recall is not None]
    macro = sum(recalls) / len(recalls) if recalls else None
    split_metrics = {}
    for split in sorted(SPLITS):
        split_hidden = sum(item.split_counts[split]["hidden"] for item in completed)
        split_recovered = sum(item.split_counts[split]["recovered"] for item in completed)
        split_metrics[split] = {
            "hidden": split_hidden,
            "recovered": split_recovered,
            "recall": split_recovered / split_hidden if split_hidden else None,
        }
    all_sources = sorted({source for item in completed for source in item.source_eligible})
    source_metrics = {}
    for source in all_sources:
        eligible = sum(item.source_eligible.get(source, 0) for item in completed)
        source_recovered = sum(item.source_recovered.get(source, 0) for item in completed)
        source_metrics[source] = {
            "eligible": eligible,
            "recovered": source_recovered,
            "recall": source_recovered / eligible if eligible else None,
        }
    payload = {
        "format_version": 1,
        "measurement": "retrospective_current_index_recovery",
        "run_id": run_id,
        "as_of": as_of.isoformat(),
        "config_digest": config_digest,
        "overall": {
            "completed_window_hidden": hidden,
            "completed_window_recovered": recovered,
            "micro_recall": micro,
            "macro_recall": macro,
            "by_evaluation_split": split_metrics,
            "by_source": source_metrics,
        },
        "windows": [item.to_dict() for item in results],
    }
    markdown = [
        "# PIP discovery historical-window backtest",
        "",
        "> This measures retrospective recovery from current source indexes; it is not a reconstruction of historical API results.",
        "",
        f"- Fixed as-of date: `{as_of.isoformat()}`",
        f"- Run ID: `{run_id}`",
        f"- Completed-window micro recall: `{micro if micro is not None else 'n/a'}`",
        f"- Completed-window macro recall: `{macro if macro is not None else 'n/a'}`",
        "",
        "| Window | Kind | Hidden | Recovered | Recall | Novel | Existing rejected |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    csv_buffer = StringIO()
    writer = csv.writer(csv_buffer, lineterminator="\n")
    writer.writerow([
        "start", "end", "kind", "hidden", "recovered", "recall", "novel",
        "full_database_existing", "misses_json", "date_disagreements_json",
    ])
    for item in results:
        recall = "" if item.recall is None else f"{item.recall:.6f}"
        markdown.append(
            f"| {item.window.start} to {item.window.end} | {item.window.kind} | {item.hidden_count} | "
            f"{item.recovered_count} | {recall or 'n/a'} | {len(item.novel_identifiers)} | {item.full_database_existing} |"
        )
        writer.writerow(
            [
                item.window.start.isoformat(),
                item.window.end.isoformat(),
                item.window.kind,
                item.hidden_count,
                item.recovered_count,
                recall,
                len(item.novel_identifiers),
                item.full_database_existing,
                json.dumps(item.misses, sort_keys=True, separators=(",", ":")),
                json.dumps(item.date_disagreements, sort_keys=True, separators=(",", ":")),
            ]
        )
    markdown.extend(["", "## Evaluation splits", "", "| Split | Hidden | Recovered | Recall |", "| --- | ---: | ---: | ---: |"])
    for split, metrics in split_metrics.items():
        markdown.append(
            f"| {split} | {metrics['hidden']} | {metrics['recovered']} | "
            f"{metrics['recall'] if metrics['recall'] is not None else 'n/a'} |"
        )
    markdown.extend(["", "## Source coverage", "", "| Source | Eligible | Recovered | Recall |", "| --- | ---: | ---: | ---: |"])
    for source, metrics in source_metrics.items():
        markdown.append(
            f"| {source} | {metrics['eligible']} | {metrics['recovered']} | "
            f"{metrics['recall'] if metrics['recall'] is not None else 'n/a'} |"
        )
    misses = [miss for item in results for miss in item.misses]
    disagreements = [value for item in results for value in item.date_disagreements]
    novel = sorted({identifier for item in results for identifier in item.novel_identifiers})
    markdown.extend(["", "## Misses", ""])
    if misses:
        markdown.extend(["| PIP LitDB ID | DOI | Reason | Expected sources |", "| --- | --- | --- | --- |"])
        for miss in misses:
            markdown.append(
                f"| {miss['pip_litdb_id']} | {miss['doi']} | {miss['reason']} | "
                f"{', '.join(miss['expected_sources'])} |"
            )
    else:
        markdown.append("No misses in the evaluated windows.")
    markdown.extend(["", "## Date disagreements", ""])
    if disagreements:
        markdown.extend(["| PIP LitDB ID | DOI | Benchmark date | Source-selected date |", "| --- | --- | --- | --- |"])
        for value in disagreements:
            markdown.append(
                f"| {value['pip_litdb_id']} | {value['doi']} | {value['benchmark_date']} | "
                f"{value['source_selected_date']} |"
            )
    else:
        markdown.append("No recovered-paper date disagreements.")
    markdown.extend(["", "## Novel candidates", ""])
    markdown.extend([f"- `{identifier}`" for identifier in novel] or ["No novel candidates."])
    markdown.append("")
    return payload, "\n".join(markdown), csv_buffer.getvalue()
