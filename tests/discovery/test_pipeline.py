from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts.discovery.config import DiscoveryConfig, QueryGroup
from scripts.discovery.deduplicate import DatabaseIndex
from scripts.discovery.models import Author, Candidate, DateValue, SourceDiagnostics, SourceResult
from scripts.discovery.pipeline import run_discovery
from scripts.discovery.sources import REQUIRED_DISCOVERY_SOURCES


def config() -> DiscoveryConfig:
    group = QueryGroup("exact", ("pyrrole-imidazole polyamide",), 8, (), 0, ("title", "abstract"), REQUIRED_DISCOVERY_SOURCES)
    return DiscoveryConfig(1, 6, 500, (group,), "a" * 64)


def empty_index() -> DatabaseIndex:
    return DatabaseIndex([], {}, {}, {})


def adapter_types(*, fail: str | None = None):
    values = {}
    for source in REQUIRED_DISCOVERY_SOURCES:
        class Adapter:
            def __init__(self, config: object, client: object, source_name: str = source) -> None:
                self.source = source_name
                self.client = client

            def search(self, start: date, end: date) -> SourceResult:
                diagnostics = self.client.diagnostics
                if self.source == fail:
                    diagnostics.complete = False
                    diagnostics.error = "fixture failure"
                    return SourceResult(self.source, [], diagnostics)
                candidate = Candidate(
                    "Pyrrole-imidazole polyamide paper",
                    [Author("Ada Example")],
                    doi="10.1234/new",
                    url="https://doi.org/10.1234/new",
                    journal="Journal",
                    document_type="research_article",
                    publication_stage="publication",
                    dates=[DateValue(date(2025, 2, 3), "online", self.source)],
                    discovered_by={self.source: self.source + "-id"},
                )
                diagnostics.result_count = 1
                return SourceResult(self.source, [candidate], diagnostics)
        values[source] = Adapter
    return values


class PipelineTests(unittest.TestCase):
    def run_pipeline(self, *, fail: str | None = None, allow_partial: bool = False):
        with tempfile.TemporaryDirectory() as directory, patch("scripts.discovery.pipeline.adapter_types", return_value=adapter_types(fail=fail)):
            return run_discovery(
                config=config(),
                exclusions=[],
                database=empty_index(),
                start=date(2025, 1, 1),
                end=date(2025, 6, 30),
                resolved_today=date(2025, 6, 30),
                cache_dir=Path(directory),
                allow_partial=allow_partial,
            )

    def test_union_is_reconciled_and_new(self) -> None:
        report = self.run_pipeline()
        self.assertEqual(report.status, "candidates")
        self.assertEqual(len(report.candidates), 1)
        self.assertEqual(set(report.candidates[0].discovered_by), set(REQUIRED_DISCOVERY_SOURCES))

    def test_required_source_failure_fails_closed(self) -> None:
        report = self.run_pipeline(fail="pubmed")
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.candidates, [])

    def test_allow_partial_is_visibly_partial(self) -> None:
        report = self.run_pipeline(fail="pubmed", allow_partial=True)
        self.assertEqual(report.status, "partial")
        self.assertEqual(len(report.candidates), 1)


if __name__ == "__main__":
    unittest.main()
