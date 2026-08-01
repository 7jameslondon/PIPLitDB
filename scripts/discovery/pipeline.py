"""Discovery orchestration with fail-closed source completeness semantics."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterable

from .config import DiscoveryConfig, Exclusion
from .deduplicate import DatabaseIndex, classify_candidates
from .http import HttpClient
from .models import Candidate, DiscoveryReport, SourceDiagnostics
from .normalize import score_candidate
from .reconcile import reconcile_candidates
from .sources import REQUIRED_DISCOVERY_SOURCES, adapter_types
from .sources.biorxiv import BioRxivEnricher
from .sources.chemrxiv import ChemRxivEnricher


def _client(
    source: str,
    cache_dir: Path,
    diagnostics: SourceDiagnostics,
    *,
    offline: bool,
) -> HttpClient:
    intervals = {
        "arxiv": 3.0,
        "pubmed": 0.34,
        "crossref": 0.1,
        "openalex": 0.1,
        "biorxiv": 0.2,
    }
    return HttpClient(
        source,
        cache_dir,
        diagnostics,
        offline=offline,
        minimum_interval=intervals.get(source, 0.0),
    )


def run_discovery(
    *,
    config: DiscoveryConfig,
    exclusions: Iterable[Exclusion],
    database: DatabaseIndex,
    start: date,
    end: date,
    resolved_today: date,
    cache_dir: Path,
    offline: bool = False,
    sources: Iterable[str] | None = None,
    allow_partial: bool = False,
) -> DiscoveryReport:
    selected = tuple(sources or REQUIRED_DISCOVERY_SOURCES)
    unsupported = set(selected) - set(REQUIRED_DISCOVERY_SOURCES)
    if unsupported:
        raise ValueError(f"unsupported source(s): {', '.join(sorted(unsupported))}")
    raw: list[Candidate] = []
    diagnostics: list[SourceDiagnostics] = []
    types = adapter_types()
    for source in selected:
        source_diagnostics = SourceDiagnostics(source)
        diagnostics.append(source_diagnostics)
        adapter = types[source](
            config,
            _client(source, cache_dir, source_diagnostics, offline=offline),
        )
        result = adapter.search(start, end)
        raw.extend(result.candidates)
    complete = all(item.complete for item in diagnostics)
    if not complete and not allow_partial:
        return DiscoveryReport(
            "failed",
            start,
            end,
            resolved_today,
            [],
            diagnostics,
            config.digest,
            warnings=["one or more required discovery sources did not complete"],
        )
    candidates = reconcile_candidates(raw)
    biorxiv_diagnostics = SourceDiagnostics("biorxiv_enrichment")
    biorxiv = BioRxivEnricher(
        _client("biorxiv", cache_dir, biorxiv_diagnostics, offline=offline)
    )
    chemrxiv = ChemRxivEnricher()
    for candidate in candidates:
        if biorxiv.applicable(candidate):
            biorxiv.enrich(candidate)
        chemrxiv.enrich(candidate)
        score_candidate(candidate, config.groups)
    if biorxiv_diagnostics.request_count or biorxiv_diagnostics.cache_hits:
        biorxiv_diagnostics.result_count = sum("biorxiv" in item.enriched_by for item in candidates)
        diagnostics.append(biorxiv_diagnostics)
    eligible_dates = [
        candidate
        for candidate in candidates
        if candidate.canonical_date and start <= candidate.canonical_date <= end
    ]
    rejected_dates = len(candidates) - len(eligible_dates)
    classified = classify_candidates(
        eligible_dates,
        database,
        exclusions,
        threshold=config.record_threshold,
    )
    actionable = [
        item for item in classified if item.disposition in {"new", "related_version"}
    ]
    status = "partial" if not complete else ("candidates" if actionable else "no_candidates")
    warnings = []
    if rejected_dates:
        warnings.append(
            f"{rejected_dates} source result(s) were excluded because their asserted publication/posting date was outside the requested window or missing"
        )
    return DiscoveryReport(
        status,
        start,
        end,
        resolved_today,
        classified,
        diagnostics,
        config.digest,
        warnings=warnings,
    )
