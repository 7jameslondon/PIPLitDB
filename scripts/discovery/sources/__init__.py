"""Discovery source adapter interface and registry."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from ..models import SourceResult


class SourceAdapter(Protocol):
    name: str

    def search(self, start: date, end: date) -> SourceResult: ...


def adapter_types() -> dict[str, type]:
    from .arxiv import ArxivAdapter
    from .crossref import CrossrefAdapter
    from .openalex import OpenAlexAdapter
    from .pubmed import PubMedAdapter

    return {
        "openalex": OpenAlexAdapter,
        "pubmed": PubMedAdapter,
        "crossref": CrossrefAdapter,
        "arxiv": ArxivAdapter,
    }


REQUIRED_DISCOVERY_SOURCES = ("openalex", "pubmed", "crossref", "arxiv")
